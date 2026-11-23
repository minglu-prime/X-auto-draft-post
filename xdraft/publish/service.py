"""Approve → schedule → publish (or dry run) → collect engagement metrics."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
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
    if draft.status not in (DraftStatus.PENDING, DraftStatus.FAILED, DraftStatus.SCHEDULED):
        raise ValueError(f"草稿 #{draft.id} 当前状态是 {draft.status}，只有待审或发布失败的草稿能批准")
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
    if draft.status == DraftStatus.SCHEDULED:
        draft.status = DraftStatus.PENDING
        draft.scheduled_at = None
        _touch(session, draft)
    return draft


def reject(session: Session, draft: Draft) -> Draft:
    if draft.status in (DraftStatus.PENDING, DraftStatus.SCHEDULED, DraftStatus.FAILED):
        draft.status = DraftStatus.REJECTED
        _touch(session, draft)
    return draft


def publish_draft(cfg: Config, session: Session, draft: Draft, client_factory: ClientFactory | None = None) -> Draft:
    """Post a scheduled draft (as a thread if it has several tweets). Never posts unapproved drafts."""
    if draft.status != DraftStatus.SCHEDULED:
        raise ValueError(f"Draft #{draft.id} is {draft.status}, not approved")
    now = utcnow()
    if not cfg.publish.enabled:
        draft.status = DraftStatus.DRY_RUN
        draft.published_at = now
        _touch(session, draft)
        _log_publication(cfg, draft, "dry_run")
        log.info("dry run: draft #%s would be posted", draft.id)
        return draft

    client = (client_factory or (lambda: make_x_client(cfg)))()
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
        draft.scheduled_at = retry
        draft.publish_error = f"被 X 限流，将在 {retry:%Y-%m-%d %H:%M} UTC 重试"
        _touch(session, draft)
        return draft
    except (XAuthError, XError, httpx.HTTPError) as e:
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


def publish_due(cfg: Config, session: Session, client_factory: ClientFactory | None = None) -> list[Draft]:
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
