"""Build style.md from your X data archive (Settings → Your account → Download an archive)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from xdraft.llm import LLM
from xdraft.pipeline.prompts import STYLE_SYSTEM
from xdraft.pipeline.schemas import StyleProfile


def find_tweets_file(path: Path) -> Path:
    """Accept the archive folder, its data/ folder, or tweets.js itself."""
    if path.is_file():
        return path
    for candidate in (path / "data" / "tweets.js", path / "tweets.js", path / "data" / "tweet.js", path / "tweet.js"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"No tweets.js found under {path}")


def load_archive_tweets(path: Path) -> list[dict]:
    """Parse `window.YTD.tweets.part0 = [...]` and keep your own original posts (no RTs or replies)."""
    raw = find_tweets_file(path).read_text(encoding="utf-8")
    data = json.loads(raw[raw.index("["):])
    tweets = []
    for entry in data:
        t = entry.get("tweet", entry)
        text = t.get("full_text") or t.get("text") or ""
        if not text or text.startswith("RT @") or t.get("in_reply_to_status_id_str") or t.get("in_reply_to_user_id_str"):
            continue
        text = re.sub(r"\s*https://t\.co/\w+\s*$", "", text).strip()  # trailing media links
        if len(text) < 15:
            continue
        tweets.append({
            "text": text,
            "likes": int(t.get("favorite_count", 0) or 0),
            "retweets": int(t.get("retweet_count", 0) or 0),
            "created_at": t.get("created_at", ""),
            "lang": t.get("lang", ""),
        })
    return tweets


def pick_samples(tweets: list[dict], n: int = 80) -> list[dict]:
    """Half best-performing, half most recent (archive order is newest first)."""
    best = sorted(tweets, key=lambda t: t["likes"] + 3 * t["retweets"], reverse=True)[: n // 2]
    seen = {id(t) for t in best}
    recent = [t for t in tweets if id(t) not in seen][: n - len(best)]
    return best + recent


def build_style(llm: LLM, tweets: list[dict], n: int = 80) -> str:
    samples = pick_samples(tweets, n)
    user = "<posts>\n" + "\n\n".join(
        f"[likes={t['likes']} reposts={t['retweets']}] {t['text']}" for t in samples
    ) + "\n</posts>"
    return llm.parse(STYLE_SYSTEM, user, StyleProfile).style_markdown.strip() + "\n"


def examples_only(tweets: list[dict], n: int = 15) -> str:
    lines = ["# My X style (examples only — edit the guide above them)", "", "Example posts:"]
    lines += [f"- {t['text']}" for t in pick_samples(tweets, n)]
    return "\n".join(lines) + "\n"
