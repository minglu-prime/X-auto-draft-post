"""Topic selection → bilingual drafts → checks → saved as pending drafts."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from xdraft.config import Config
from xdraft.llm import LLM
from xdraft.models import Draft, DraftStatus, Item, Topic
from xdraft.pipeline import prompts
from xdraft.pipeline.check import check_post
from xdraft.pipeline.schemas import BilingualDraft, PostVersion, TopicPlan, TopicProposal
from xdraft.timeutil import to_utc, utcnow

log = logging.getLogger(__name__)

LANG_NAMES = {"zh": "Chinese (简体中文)", "en": "English"}


def load_style(cfg: Config) -> str:
    path = cfg.resolve_path(cfg.draft.style_path)
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return prompts.DEFAULT_STYLE


def recent_posts(session: Session, days: int = 30) -> list[Draft]:
    cutoff = utcnow() - timedelta(days=days)
    return list(session.exec(
        select(Draft).where(
            col(Draft.status).in_([DraftStatus.PUBLISHED, DraftStatus.DRY_RUN, DraftStatus.SCHEDULED]),
            Draft.updated_at >= cutoff,
        )
    ).all())


def performance_summary(session: Session, limit: int = 5) -> str:
    """Best and worst published posts by engagement, fed back into topic selection."""
    rows = [d for d in session.exec(select(Draft).where(Draft.status == DraftStatus.PUBLISHED)).all() if d.metrics]
    if not rows:
        return "(no engagement data yet)"

    def score(d: Draft) -> int:
        m = d.metrics
        return int(m.get("like_count", 0)) + 3 * int(m.get("retweet_count", 0)) + 2 * int(m.get("reply_count", 0))

    rows.sort(key=score, reverse=True)
    picked = rows[:limit] + [r for r in rows[-limit:] if r not in rows[:limit]]
    lines = []
    for d in picked:
        m = d.metrics
        lines.append(
            f"- likes {m.get('like_count', 0)}, reposts {m.get('retweet_count', 0)}, "
            f"replies {m.get('reply_count', 0)}, views {m.get('impression_count', '?')}: "
            + " ".join(d.tweets[0].split())[:140]
        )
    return "\n".join(lines)


def select_topics(llm: LLM, cfg: Config, items: list[Item], recent: list[str], performance: str) -> list[TopicProposal]:
    system = prompts.SELECT_SYSTEM.format(
        n=cfg.draft.topics_per_run,
        recent="\n".join(f"- {r}" for r in recent[:30]) or "(none)",
        performance=performance,
    )
    plan = llm.parse(system, prompts.format_items(items, cfg.draft.item_body_chars), TopicPlan)
    valid_ids = {it.id for it in items}
    topics = []
    for t in plan.topics:
        t.item_ids = [i for i in t.item_ids if i in valid_ids]
        if t.item_ids:
            topics.append(t)
        else:
            log.warning("dropping topic %r: it cites no known items", t.title)
    return topics[: cfg.draft.topics_per_run]


def write_drafts(llm: LLM, cfg: Config, topic: TopicProposal | Topic, items: list[Item], style: str) -> BilingualDraft:
    system = prompts.DRAFT_SYSTEM.format(style=style, thread_max=cfg.draft.thread_max_tweets)
    user = (
        f"Topic: {topic.title}\nAngle: {topic.angle}\nWhy now: {topic.why}\n\n"
        + prompts.format_items(items, cfg.draft.item_body_chars)
    )
    return llm.parse(system, user, BilingualDraft)


def rewrite_post(llm: LLM, cfg: Config, draft: Draft, items: list[Item], instruction: str, style: str) -> PostVersion:
    system = prompts.REWRITE_SYSTEM.format(
        lang_name=LANG_NAMES.get(draft.lang, draft.lang), style=style, thread_max=cfg.draft.thread_max_tweets
    )
    current = "\n---\n".join(draft.tweets)
    user = (
        f"Instruction: {instruction}\n\nCurrent post (tweets separated by ---):\n{current}\n\n"
        f"Current source URLs: {', '.join(draft.source_urls) or '(none)'}\n\n"
        + prompts.format_items(items, cfg.draft.item_body_chars)
    )
    return llm.parse(system, user, PostVersion)


def material_for(items: list[Item]) -> tuple[str, list[str]]:
    text = "\n".join(
        f"{it.title}\n{it.body}\n{(it.meta or {}).get('fulltext', '')}" for it in items
    )
    return text, [it.url for it in items if it.url] + [
        u for it in items for u in (it.meta or {}).get("urls", [])
    ]


def run_checks(cfg: Config, session: Session, draft: Draft, items: list[Item]) -> list[str]:
    text, urls = material_for(items)
    recent = [d.text for d in recent_posts(session) if d.id != draft.id]
    return check_post(
        draft.tweets, draft.source_urls, text, urls, recent,
        thread_max=cfg.draft.thread_max_tweets, duplicate_threshold=cfg.draft.duplicate_threshold,
    )


def items_for_topic(session: Session, topic: Topic) -> list[Item]:
    if not topic.item_ids:
        return []
    return list(session.exec(select(Item).where(col(Item.id).in_(topic.item_ids))).all())


def generate(cfg: Config, llm: LLM, session: Session, since: datetime, n_topics: int | None = None) -> list[Draft]:
    if n_topics:
        cfg = cfg.model_copy(deep=True)
        cfg.draft.topics_per_run = n_topics
    items = list(session.exec(
        select(Item).where(Item.ts >= to_utc(since)).order_by(col(Item.ts).desc()).limit(cfg.draft.max_items)
    ).all())
    if not items:
        return []
    recent = [" ".join(d.tweets[0].split())[:120] for d in recent_posts(session) if d.tweets]
    proposals = select_topics(llm, cfg, items, recent, performance_summary(session))
    by_id = {it.id: it for it in items}
    style = load_style(cfg)
    created: list[Draft] = []
    for proposal in proposals:
        topic_items = [by_id[i] for i in proposal.item_ids]
        try:
            bilingual = write_drafts(llm, cfg, proposal, topic_items, style)
        except Exception as e:  # noqa: BLE001 - one bad topic must not lose the others
            log.warning("drafting %r failed: %s", proposal.title, e)
            continue
        now = utcnow()
        topic = Topic(title=proposal.title, angle=proposal.angle, why=proposal.why,
                      score=proposal.score, item_ids=proposal.item_ids, created_at=now)
        session.add(topic)
        session.flush()
        for lang in ("zh", "en"):
            version: PostVersion = getattr(bilingual, lang)
            draft = Draft(topic_id=topic.id, lang=lang, tweets=[t.strip() for t in version.tweets],
                          source_urls=version.source_urls, created_at=now, updated_at=now)
            draft.warnings = run_checks(cfg, session, draft, topic_items)
            session.add(draft)
            created.append(draft)
        session.commit()
    for d in created:
        session.refresh(d)
    return created
