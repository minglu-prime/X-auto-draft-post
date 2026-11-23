"""The content pipeline.

    suggest_topics:  trends + own usage + material → topic suggestions (wait for the author to pick)
    produce_drafts:  picked topic → write (zh+en) → polish → optimize (+ re-optimize while hard checks fail)

Publishing (approve → next free slot → post) lives in xdraft.publish.service.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from xdraft.config import Config
from xdraft.llm import LLM, LLMError
from xdraft.models import Draft, DraftStatus, Item, Topic, TopicStatus
from xdraft.pipeline import prompts
from xdraft.pipeline.check import check_post
from xdraft.pipeline.schemas import (
    BilingualDraft,
    OptimizedDraft,
    PostVersion,
    SuggestionPlan,
)
from xdraft.pipeline.trends import TrendTerm, format_trends, select_material, trend_terms, usage_summary
from xdraft.timeutil import to_utc, utcnow

log = logging.getLogger(__name__)

LANGS = ("zh", "en")
LANG_NAMES = {"zh": "Chinese (简体中文)", "en": "English"}


# ---- shared helpers ---------------------------------------------------------------

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


def items_in_window(session: Session, since: datetime) -> list[Item]:
    return list(session.exec(select(Item).where(Item.ts >= to_utc(since)).order_by(col(Item.ts).desc())).all())


def items_for_topic(session: Session, topic: Topic) -> list[Item]:
    if not topic.item_ids:
        return []
    return list(session.exec(select(Item).where(col(Item.id).in_(topic.item_ids))).all())


def material_for(items: list[Item]) -> tuple[str, list[str]]:
    text = "\n".join(f"{it.title}\n{it.body}\n{(it.meta or {}).get('fulltext', '')}" for it in items)
    urls = [it.url for it in items if it.url] + [u for it in items for u in (it.meta or {}).get("urls", [])]
    return text, urls


def run_checks(cfg: Config, session: Session, draft: Draft, items: list[Item]) -> list[str]:
    text, urls = material_for(items)
    recent = [d.text for d in recent_posts(session) if d.id is None or d.id != draft.id]
    return check_post(
        draft.tweets, draft.source_urls, text, urls, recent,
        thread_max=cfg.draft.thread_max_tweets, duplicate_threshold=cfg.draft.duplicate_threshold,
    )


def is_hard_warning(w: str) -> bool:
    """Problems that would block approval or publishing."""
    return ("weighted characters" in w) or w.startswith("Thread has") or w.startswith("Draft is empty")


# ---- 1. topic suggestions ---------------------------------------------------------

@dataclass
class TrendSnapshot:
    terms: list[TrendTerm]
    usage: str
    n_items: int


def trend_snapshot(cfg: Config, session: Session, since: datetime) -> TrendSnapshot:
    items = items_in_window(session, since)
    return TrendSnapshot(trend_terms(items, cfg.pipeline.trend_terms), usage_summary(items), len(items))


def suggest_topics(cfg: Config, llm: LLM, session: Session, since: datetime, n: int | None = None) -> list[Topic]:
    items = items_in_window(session, since)
    if not items:
        return []
    terms = trend_terms(items, cfg.pipeline.trend_terms)
    material = select_material(items, terms, cfg.pipeline.personal_items, cfg.draft.max_items)
    recent = [" ".join(d.tweets[0].split())[:120] for d in recent_posts(session) if d.tweets]
    recent += [t.title for t in session.exec(
        select(Topic).where(Topic.status == TopicStatus.SUGGESTED, Topic.created_at >= utcnow() - timedelta(days=7))
    ).all()]
    n = n or cfg.pipeline.suggest_count
    system = prompts.SUGGEST_SYSTEM.format(
        n=n, recent="\n".join(f"- {r}" for r in recent[:40]) or "(none)", performance=performance_summary(session),
    )
    user = (
        f"<trends>\n{format_trends(terms)}\n</trends>\n\n<usage>\n{usage_summary(items)}\n</usage>\n\n"
        + prompts.format_items(material, cfg.draft.item_body_chars)
    )
    plan = llm.parse(system, user, SuggestionPlan)
    valid = {it.id for it in material}
    now, topics = utcnow(), []
    for s in plan.topics[:n]:
        ids = [i for i in s.item_ids if i in valid]
        if not ids:
            log.warning("dropping suggestion %r: it cites no known items", s.title)
            continue
        topic = Topic(title=s.title, angle=s.angle, why=s.why_now, my_connection=s.my_connection,
                      format=s.format, trend_terms=s.trend_terms, score=s.score, item_ids=ids,
                      status=TopicStatus.SUGGESTED, created_at=now)
        session.add(topic)
        topics.append(topic)
    session.commit()
    for t in topics:
        session.refresh(t)
    return topics


# ---- 2. write → polish → optimize --------------------------------------------------

def _post_json(versions: dict[str, PostVersion]) -> str:
    return json.dumps({k: {"tweets": v.tweets, "source_urls": v.source_urls} for k, v in versions.items()},
                      ensure_ascii=False, indent=1)


def _topic_brief(topic: Topic) -> str:
    return (f"Topic: {topic.title}\nAngle: {topic.angle}\nWhy now: {topic.why}\n"
            f"Author's own experience: {topic.my_connection or '(none)'}\nFormat: {topic.format}\n")


def write_drafts(llm: LLM, cfg: Config, topic: Topic, items: list[Item], style: str) -> BilingualDraft:
    system = prompts.DRAFT_SYSTEM.format(style=style, thread_max=cfg.draft.thread_max_tweets)
    return llm.parse(system, _topic_brief(topic) + "\n" + prompts.format_items(items, cfg.draft.item_body_chars),
                     BilingualDraft)


def polish_drafts(llm: LLM, cfg: Config, topic: Topic, versions: dict[str, PostVersion], items: list[Item],
                  style: str) -> BilingualDraft:
    system = prompts.POLISH_SYSTEM.format(style=style, thread_max=cfg.draft.thread_max_tweets)
    user = (_topic_brief(topic) + "\n<draft>\n" + _post_json(versions) + "\n</draft>\n\n"
            + prompts.format_items(items, cfg.draft.item_body_chars))
    return llm.parse(system, user, BilingualDraft)


def optimize_drafts(llm: LLM, cfg: Config, topic: Topic, versions: dict[str, PostVersion],
                    warnings: dict[str, list[str]], items: list[Item], style: str) -> OptimizedDraft:
    system = prompts.OPTIMIZE_SYSTEM.format(style=style, thread_max=cfg.draft.thread_max_tweets)
    checks = "\n".join(f"{lang}: " + ("; ".join(ws) if ws else "no problems") for lang, ws in warnings.items())
    user = (_topic_brief(topic) + "\n<draft>\n" + _post_json(versions) + "\n</draft>\n\n<checks>\n" + checks
            + "\n</checks>\n\n" + prompts.format_items(items, cfg.draft.item_body_chars))
    return llm.parse(system, user, OptimizedDraft)


def produce_drafts(cfg: Config, llm: LLM, session: Session, topic: Topic) -> list[Draft]:
    """Run the full writing chain for one picked topic and save a zh and an en draft."""
    items = items_for_topic(session, topic)
    style = load_style(cfg)
    now = utcnow()
    written = write_drafts(llm, cfg, topic, items, style)
    versions: dict[str, PostVersion] = {lang: getattr(written, lang) for lang in LANGS}
    revisions = {lang: [{"stage": "written", "tweets": versions[lang].tweets}] for lang in LANGS}
    stage, notes, alt_hooks, stage_errors = "written", {}, {lang: [] for lang in LANGS}, []

    def checks_for(vs: dict[str, PostVersion]) -> dict[str, list[str]]:
        return {lang: run_checks(cfg, session, Draft(lang=lang, tweets=v.tweets, source_urls=v.source_urls,
                                                     created_at=now, updated_at=now), items)
                for lang, v in vs.items()}

    try:
        polished = polish_drafts(llm, cfg, topic, versions, items, style)
        versions = {lang: getattr(polished, lang) for lang in LANGS}
        for lang in LANGS:
            revisions[lang].append({"stage": "polished", "tweets": versions[lang].tweets})
        stage = "polished"
    except LLMError as e:
        stage_errors.append(f"Polish step failed: {e}")

    if stage == "polished":
        for round_no in range(1 + max(0, cfg.pipeline.optimize_rounds)):
            warnings = checks_for(versions)
            if round_no > 0 and not any(is_hard_warning(w) for ws in warnings.values() for w in ws):
                break
            try:
                optimized = optimize_drafts(llm, cfg, topic, versions, warnings, items, style)
            except LLMError as e:
                stage_errors.append(f"Optimize step failed: {e}")
                break
            for lang in LANGS:
                ov = getattr(optimized, lang)
                versions[lang] = PostVersion(tweets=ov.tweets, source_urls=ov.source_urls)
                alt_hooks[lang] = ov.alt_hooks[:3] or alt_hooks[lang]
                notes[lang] = ov.notes
                revisions[lang].append({"stage": "optimized", "tweets": ov.tweets, "notes": ov.notes})
            stage = "optimized"

    drafts = []
    for lang in LANGS:
        v = versions[lang]
        d = Draft(topic_id=topic.id, lang=lang, tweets=[t.strip() for t in v.tweets if t.strip()],
                  source_urls=v.source_urls, stage=stage, revisions=revisions[lang],
                  alt_hooks=alt_hooks[lang], created_at=now, updated_at=now)
        d.warnings = stage_errors + run_checks(cfg, session, d, items)
        session.add(d)
        drafts.append(d)
    topic.status = TopicStatus.DRAFTED
    session.add(topic)
    session.commit()
    for d in drafts:
        session.refresh(d)
    return drafts


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
