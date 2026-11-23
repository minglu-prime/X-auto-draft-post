"""Collect AI articles: RSS/Atom feeds (blogs, arXiv) and Hacker News front-page stories."""

from __future__ import annotations

import calendar
import html
import logging
import re
from datetime import datetime, timezone

import feedparser
import httpx

from xdraft.collectors.base import CollectedItem, CollectResult
from xdraft.config import Config, FeedConfig
from xdraft.timeutil import utcnow

log = logging.getLogger(__name__)

SOURCE = "articles"
USER_AGENT = "xdraft/0.1 (personal feed reader)"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    # Unescape first so entity-encoded markup (&lt;p&gt;) is stripped too.
    text = _TAG_RE.sub(" ", html.unescape(text or ""))
    return " ".join(html.unescape(text).split())


def keyword_matcher(keywords: list[str]):
    if not keywords:
        return lambda _text: True
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(k) for k in keywords) + r")(?:s|es)?\b", re.I
    )
    return lambda text: bool(pattern.search(text or ""))


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    return None


def collect_feed(client: httpx.Client, feed: FeedConfig, since: datetime, matches) -> list[CollectedItem]:
    resp = client.get(feed.url)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    items = []
    for entry in parsed.entries[: feed.max_items]:
        ts = _entry_time(entry) or utcnow()
        if ts < since:
            continue
        title = strip_html(entry.get("title", ""))
        summary = strip_html(entry.get("summary", ""))
        if feed.filter and not matches(f"{title} {summary}"):
            continue
        link = entry.get("link")
        items.append(CollectedItem(
            source=SOURCE,
            source_id=str(entry.get("id") or link or title),
            ts=ts,
            title=title or "(untitled)",
            body=summary,
            url=link,
            meta={
                "feed": feed.name,
                "author": entry.get("author"),
                "tags": [t.get("term") for t in entry.get("tags", []) if t.get("term")],
            },
        ))
    return items


def collect_hackernews(client: httpx.Client, min_points: int, since: datetime, matches) -> list[CollectedItem]:
    resp = client.get(HN_SEARCH_URL, params={
        "tags": "story",
        "numericFilters": f"created_at_i>{int(since.timestamp())},points>={min_points}",
        "hitsPerPage": 1000,
    })
    resp.raise_for_status()
    items = []
    for hit in resp.json().get("hits", []):
        title = hit.get("title") or ""
        if not matches(title):
            continue
        hn_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
        items.append(CollectedItem(
            source=SOURCE,
            source_id=f"hn:{hit['objectID']}",
            ts=datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc),
            title=title,
            body=strip_html(hit.get("story_text") or ""),
            url=hit.get("url") or hn_url,
            meta={
                "feed": "Hacker News",
                "hn_url": hn_url,
                "points": hit.get("points"),
                "num_comments": hit.get("num_comments"),
            },
        ))
    return items


def _add_fulltext(client: httpx.Client, items: list[CollectedItem], max_chars: int, errors: list[str]) -> None:
    try:
        import trafilatura
    except ImportError:
        errors.append("fetch_fulltext is on but trafilatura is not installed (pip install 'xdraft[fulltext]')")
        return
    for item in items:
        if not item.url:
            continue
        try:
            resp = client.get(item.url)
            resp.raise_for_status()
            text = trafilatura.extract(resp.text) or ""
        except Exception as e:  # noqa: BLE001 - one bad page must not stop the run
            errors.append(f"fulltext {item.url}: {e}")
            continue
        if text:
            item.meta["fulltext"] = text[:max_chars]


def collect(cfg: Config, since: datetime, client: httpx.Client | None = None) -> CollectResult:
    ac = cfg.articles
    result = CollectResult()
    matches = keyword_matcher(ac.keywords)
    own_client = client is None
    client = client or httpx.Client(
        timeout=ac.timeout_seconds, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )
    try:
        for feed in ac.feeds:
            try:
                result.items += collect_feed(client, feed, since, matches)
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"feed {feed.name}: {e}")
        if ac.hackernews.enabled:
            try:
                result.items += collect_hackernews(client, ac.hackernews.min_points, since, matches)
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"hacker news: {e}")
        if ac.fetch_fulltext:
            _add_fulltext(client, result.items, ac.fulltext_max_chars, result.errors)
    finally:
        if own_client:
            client.close()
    # The same story can appear in several feeds; keep the first per source_id.
    seen, unique = set(), []
    for item in result.items:
        if item.source_id not in seen:
            seen.add(item.source_id)
            unique.append(item)
    result.items = unique
    return result
