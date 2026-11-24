"""Approve → schedule → publish (or dry run) → collect engagement metrics."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import update
from sqlmodel import Session, col, select

from xdraft import media
from xdraft.config import Config
from xdraft.models import Draft, DraftStatus
from xdraft.pipeline.check import MAX_WEIGHTED, weighted_length
from xdraft.publish.x import XAuthError, XClient, XError, XRateLimited
from xdraft.timeutil import to_utc, utcnow

log = logging.getLogger(__name__)

ClientFactory = Callable[[], XClient]


def make_x_client(cfg: Config) -> XClient:
    return XClient(cfg.publish, cfg.resolve_path(cfg.publish.token_path))


def _log_publication(cfg: Config, draft: Draft, mode: str) -> None:
    path = cfg.resolve_path("data/publish.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "at": utcnow().isoformat(), "mode": mode, "draft_id": draft.id, "lang": draft.lang,
            "tweets": draft.tweets, "images": media.images_for(draft), "tweet_ids": draft.tweet_ids,
            "error": draft.publish_error,
        }, ensure_ascii=False) + "\n")


def _touch(session: Session, draft: Draft) -> None:
    draft.updated_at = utcnow()
    session.add(draft)
    session.commit()
    session.refresh(draft)


def slot_times(cfg: Config, after: datetime, days: int = 14) -> list[datetime]:
    """Upcoming posting slots (configured local times) as UTC datetimes, earliest first."""
    tz = ZoneInfo(cfg.timezone)
    local_after = to_utc(after).astimezone(tz)
    slots = []
    for spec in cfg.publish.slots:
        hh, mm = (int(x) for x in spec.split(":"))
        for day in range(days + 1):
            d = local_after.date() + timedelta(days=day)
            slots.append(datetime.combine(d, time(hh, mm), tzinfo=tz).astimezone(ZoneInfo("UTC")))
    return sorted(s for s in slots if s > to_utc(after))


def next_free_slot(cfg: Config, session: Session, after: datetime | None = None) -> datetime | None:
    """First slot at least 5 minutes away that has fewer than max_per_slot posts."""
    start = (after or utcnow()) + timedelta(minutes=5)
    taken = [to_utc(d.scheduled_at) for d in session.exec(select(Draft).where(
        col(Draft.status).in_([DraftStatus.SCHEDULED, DraftStatus.PUBLISHED, DraftStatus.DRY_RUN]),
        col(Draft.scheduled_at) >= start - timedelta(minutes=1),
    )).all() if d.scheduled_at]
    for slot in slot_times(cfg, start):
        if sum(1 for t in taken if abs((t - slot).total_seconds()) < 60) < cfg.publish.max_per_slot:
            return slot
    return None


def approve(session: Session, draft: Draft, when: datetime | None = None) -> Draft:
    if draft.status not in (DraftStatus.PENDING, DraftStatus.FAILED, DraftStatus.SCHEDULED, DraftStatus.DUE):
        raise ValueError(f"草稿 #{draft.id} 当前状态是 {draft.status}，只有待审、待发或发布失败的草稿能批准")
    too_long = [i for i, t in enumerate(draft.tweets, 1) if weighted_length(t) > MAX_WEIGHTED]
    if too_long or not draft.tweets:
        raise ValueError(f"第 {too_long[0]} 条推文超过 {MAX_WEIGHTED} 字符，请先删短"
                         if too_long else "草稿是空的")
    draft.status = DraftStatus.SCHEDULED
    draft.scheduled_at = to_utc(when) if when else utcnow()
    draft.publish_error = None
    _touch(session, draft)
    return draft


def unschedule(session: Session, draft: Draft) -> Draft:
    if draft.status in (DraftStatus.SCHEDULED, DraftStatus.DUE):
        draft.status = DraftStatus.PENDING
        draft.scheduled_at = None
        _touch(session, draft)
    return draft


def reject(session: Session, draft: Draft) -> Draft:
    """Skip a draft. A scheduled/due draft keeps its slot time so the skip can be undone exactly."""
    if draft.status in (DraftStatus.PENDING, DraftStatus.SCHEDULED, DraftStatus.DUE, DraftStatus.FAILED):
        if draft.status in (DraftStatus.PENDING, DraftStatus.FAILED):
            draft.scheduled_at = None
        draft.status = DraftStatus.REJECTED
        _touch(session, draft)
    return draft


def restore(session: Session, draft: Draft) -> Draft:
    """Undo a skip: back to where it was (scheduled, due, or review)."""
    if draft.status == DraftStatus.REJECTED:
        if draft.scheduled_at is None:
            draft.status = DraftStatus.PENDING
        elif to_utc(draft.scheduled_at) > utcnow():
            draft.status = DraftStatus.SCHEDULED
        else:
            draft.status = DraftStatus.DUE
        _touch(session, draft)
    return draft


_TWEET_URL = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/[^/\s]+/status/(\d+)")


def mark_posted(session: Session, draft: Draft, url: str = "") -> Draft:
    """The author posted it by hand. An optional tweet link lets History link to it."""
    url = url.strip()
    m = _TWEET_URL.match(url) if url else None
    if url and not m:
        raise ValueError("这不像 X 的推文链接，应该是 https://x.com/用户名/status/数字 这样的")
    if draft.status != DraftStatus.DUE:
        raise ValueError("只有「该发了」的草稿能标记为已发出；先刷新页面看看它现在的状态")
    draft.status = DraftStatus.PUBLISHED
    draft.published_at = utcnow()
    draft.tweet_ids = [m.group(1)] if m else []
    draft.metrics = {"posted_by": "hand"}
    draft.publish_error = None
    _touch(session, draft)
    return draft


def unmark_posted(session: Session, draft: Draft) -> Draft:
    """Undo mark_posted: back to waiting for a manual post. Only for a hand-marked post; a post
    xdraft made itself is real on X and cannot be "unposted" here."""
    if draft.status == DraftStatus.PUBLISHED and (draft.metrics or {}).get("posted_by") == "hand":
        draft.status = DraftStatus.DUE
        draft.published_at = None
        draft.tweet_ids = []
        _touch(session, draft)
    return draft


def auto_posting(cfg: Config, client: XClient) -> bool:
    """xdraft posts by itself only when posting is enabled and X is connected; otherwise the author posts by hand."""
    return bool(cfg.publish.enabled and client.connected)


def publish_draft(cfg: Config, session: Session, draft: Draft, client_factory: ClientFactory | None = None) -> Draft:
    """Post a scheduled draft (as a thread if it has several tweets), or mark it due for a manual post.

    Never posts unapproved drafts."""
    if draft.status != DraftStatus.SCHEDULED:
        raise ValueError(f"Draft #{draft.id} is {draft.status}, not approved")
    # Claim it atomically: the dashboard's scheduler and the Windows scheduled task may both run;
    # only the one that flips scheduled -> publishing may touch X.
    claimed = session.execute(
        update(Draft).where(Draft.id == draft.id, Draft.status == DraftStatus.SCHEDULED)
        .values(status=DraftStatus.PUBLISHING, updated_at=utcnow())
    ).rowcount
    session.commit()
    session.refresh(draft)
    if claimed != 1:
        return draft
    now = utcnow()
    client = (client_factory or (lambda: make_x_client(cfg)))()
    if not auto_posting(cfg, client):
        return _to_manual(cfg, session, draft)
    # It may have been edited after approval: check again before anything goes out, so a thread
    # never stops half-way on a tweet X would reject.
    too_long = [i for i, t in enumerate(draft.tweets, 1) if weighted_length(t) > MAX_WEIGHTED]
    if too_long or not draft.tweets:
        draft.status = DraftStatus.FAILED
        draft.publish_error = (f"第 {too_long[0]} 条超过 {MAX_WEIGHTED} 字符，没有发出；改短后重新排期"
                               if too_long else "草稿是空的，没有发出")
        _touch(session, draft)
        _log_publication(cfg, draft, "failed")
        return draft

    ids = list(draft.tweet_ids or [])  # resume a thread that failed half-way
    images = media.images_for(draft)
    try:
        for i in range(len(ids), len(draft.tweets)):
            media_ids = []
            for name in images[i]:
                path = media.media_dir(cfg, draft.id) / name
                if not path.is_file():
                    raise XError(f"配图文件不见了：{path}")
                media_ids.append(client.upload_image(name, path.read_bytes(), media.mime_for(name)))
            ids.append(client.post_tweet(draft.tweets[i], reply_to=ids[-1] if ids else None,
                                         media_ids=media_ids or None))
            draft.tweet_ids = list(ids)
            _touch(session, draft)
    except XRateLimited as e:
        retry = max(e.retry_at or now, now + timedelta(minutes=cfg.publish.retry_minutes_on_rate_limit))
        draft.status = DraftStatus.SCHEDULED
        draft.scheduled_at = retry
        draft.publish_error = f"被 X 限流，将在 {retry:%Y-%m-%d %H:%M} UTC 重试"
        _touch(session, draft)
        return draft
    except XAuthError as e:
        if not ids:  # access was revoked and nothing went out: let the author post it by hand
            log.warning("X rejected xdraft's access (%s); draft #%s falls back to manual posting", e, draft.id)
            draft.publish_error = f"X 授权失效，改为手动发布：{e}"
            return _to_manual(cfg, session, draft)
        draft.status = DraftStatus.FAILED
        draft.publish_error = f"已发出前 {len(ids)} 条后 X 授权失效：{e}"
        _touch(session, draft)
        _log_publication(cfg, draft, "failed")
        return draft
    except (XError, httpx.HTTPError) as e:
        draft.status = DraftStatus.FAILED
        draft.publish_error = str(e)
        _touch(session, draft)
        _log_publication(cfg, draft, "failed")
        return draft
    draft.status = DraftStatus.PUBLISHED
    draft.published_at = utcnow()
    draft.publish_error = None
    _touch(session, draft)
    _log_publication(cfg, draft, "published")
    return draft


def _to_manual(cfg: Config, session: Session, draft: Draft) -> Draft:
    draft.status = DraftStatus.DUE
    _touch(session, draft)
    _log_publication(cfg, draft, "manual_due")
    log.info("draft #%s is due: post it by hand", draft.id)
    return draft


STALE_PUBLISHING = timedelta(minutes=10)


def publish_due(cfg: Config, session: Session, client_factory: ClientFactory | None = None) -> list[Draft]:
    # A publisher that died mid-post leaves the draft claimed; surface it instead of hiding it forever.
    for d in session.exec(select(Draft).where(Draft.status == DraftStatus.PUBLISHING)).all():
        if to_utc(d.updated_at) < utcnow() - STALE_PUBLISHING:
            d.status = DraftStatus.FAILED
            d.publish_error = (f"发布中断（已发出 {len(d.tweet_ids or [])} 条），先去 X 上确认，再重新排期接着发"
                               if d.tweet_ids else "发布中断，没有发出；可以重新排期")
            _touch(session, d)
    due = session.exec(
        select(Draft).where(Draft.status == DraftStatus.SCHEDULED, col(Draft.scheduled_at) <= utcnow())
        .order_by(col(Draft.scheduled_at))
    ).all()
    return [publish_draft(cfg, session, d, client_factory) for d in due]


METRIC_KEYS = ("like_count", "retweet_count", "reply_count", "quote_count", "impression_count", "bookmark_count")


def refresh_metrics(cfg: Config, session: Session, client_factory: ClientFactory | None = None, days: int = 30) -> int:
    """Fetch public metrics for recent published posts; a thread's metrics are summed."""
    cutoff = utcnow() - timedelta(days=days)
    drafts = [d for d in session.exec(
        select(Draft).where(Draft.status == DraftStatus.PUBLISHED, col(Draft.published_at) >= cutoff)
    ).all() if d.tweet_ids]
    if not drafts:
        return 0
    client = (client_factory or (lambda: make_x_client(cfg)))()
    metrics = client.public_metrics([i for d in drafts for i in d.tweet_ids])
    for d in drafts:
        total = {k: sum(int(metrics.get(i, {}).get(k, 0) or 0) for i in d.tweet_ids) for k in METRIC_KEYS}
        total["fetched_at"] = utcnow().isoformat()
        d.metrics = total
        session.add(d)
    session.commit()
    return len(drafts)
