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

import httpx
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, col, select

from xdraft import voice

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
        text = path.read_text(encoding="utf-8")
        text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("<!--")).strip()
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
    topic = session.get(Topic, draft.topic_id) if draft.topic_id else None
    if topic:  # the author's own experience and take are valid sources too
        text += f"\n{topic.angle}\n{topic.why}\n{topic.my_connection}\n{topic.user_take}"
    recent = [d.text for d in recent_posts(session) if d.id is None or d.id != draft.id]
    return check_post(
        draft.tweets, draft.source_urls, text, urls, recent,
        thread_max=cfg.draft.thread_max_tweets, duplicate_threshold=cfg.draft.duplicate_threshold,
        lang=draft.lang,
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
    brief = (f"Topic: {topic.title}\nAngle: {topic.angle}\nWhy now: {topic.why}\n"
             f"Author's own experience: {topic.my_connection or '(none)'}\n")
    if (topic.user_take or "").strip():
        brief += f"Author's own take (the backbone of the post; argue exactly this): {topic.user_take.strip()}\n"
    return brief


def _format_rule(cfg: Config, topic: Topic) -> str:
    if (topic.format or "thread") in ("quick", "single"):
        return prompts.QUICK_RULE
    return prompts.THREAD_RULE.format(min=cfg.draft.thread_min_tweets, max=cfg.draft.thread_max_tweets)


def voice_for(cfg: Config, topic_text: str) -> str:
    """The author's real writing (both languages) plus a few excerpts from writers they like."""
    own_all, refs_all = voice.load_own(cfg), voice.load_references(cfg)
    n = cfg.voice.own_examples
    own = voice.pick_examples(own_all, topic_text, "zh", n - n // 2)
    own += [s for s in voice.pick_examples(own_all, topic_text, "en", n // 2) if s not in own]
    refs = voice.pick_examples(refs_all, topic_text, "zh", cfg.voice.reference_excerpts)
    blocks = [b for b in (voice.format_own(own), voice.format_references(refs)) if b]
    return "\n\n".join(blocks) or "(No examples of the author's writing yet: follow the style notes closely.)"


def _material(cfg: Config, items: list[Item]) -> str:
    return prompts.format_items(items, cfg.draft.item_body_chars, cfg.draft.fulltext_chars)


def write_drafts(llm: LLM, cfg: Config, topic: Topic, items: list[Item], style: str, voice_text: str) -> BilingualDraft:
    system = prompts.DRAFT_SYSTEM.format(voice=voice_text, style=style, format_rule=_format_rule(cfg, topic),
                                         tells=prompts.AI_TELLS)
    return llm.parse(system, _topic_brief(topic) + "\n" + _material(cfg, items), BilingualDraft,
                     temperature=cfg.llm.temperature_write)


def polish_drafts(llm: LLM, cfg: Config, topic: Topic, versions: dict[str, PostVersion], items: list[Item],
                  style: str, voice_text: str) -> OptimizedDraft:
    system = prompts.POLISH_SYSTEM.format(voice=voice_text, style=style, min=cfg.draft.thread_min_tweets,
                                          tells=prompts.AI_TELLS)
    user = _topic_brief(topic) + "\n<draft>\n" + _post_json(versions) + "\n</draft>\n\n" + _material(cfg, items)
    return llm.parse(system, user, OptimizedDraft, temperature=cfg.llm.temperature_edit)


def optimize_drafts(llm: LLM, cfg: Config, topic: Topic, versions: dict[str, PostVersion],
                    warnings: dict[str, list[str]], items: list[Item], style: str, voice_text: str) -> OptimizedDraft:
    """Fix-only pass: runs only when deterministic checks found concrete problems."""
    system = prompts.OPTIMIZE_SYSTEM.format(voice=voice_text, max=cfg.draft.thread_max_tweets, tells=prompts.AI_TELLS)
    checks = "\n".join(f"{lang}: " + ("; ".join(ws) if ws else "no problems") for lang, ws in warnings.items())
    user = ("<draft>\n" + _post_json(versions) + "\n</draft>\n\n<checks>\n" + checks + "\n</checks>\n\n"
            + _material(cfg, items))
    return llm.parse(system, user, OptimizedDraft, temperature=cfg.llm.temperature_edit)


def fetch_fulltext(cfg: Config, session: Session, items: list[Item], client: httpx.Client | None = None) -> None:
    """Full article text for the items of a picked topic (only these, only once)."""
    if not cfg.articles.fetch_fulltext:
        return
    todo = [it for it in items if it.source == "articles" and it.url and not (it.meta or {}).get("fulltext")]
    if not todo:
        return
    from xdraft.collectors.articles import USER_AGENT, _add_fulltext

    own = client is None
    client = client or httpx.Client(timeout=cfg.articles.timeout_seconds, follow_redirects=True,
                                    headers={"User-Agent": USER_AGENT})
    errors: list[str] = []
    try:
        for it in todo:
            it.meta = dict(it.meta or {})
        _add_fulltext(client, todo, cfg.articles.fulltext_max_chars, errors)
    finally:
        if own:
            client.close()
    for it in todo:
        flag_modified(it, "meta")
        session.add(it)
    session.commit()
    for e in errors:
        log.info("full text: %s", e)


def _needs_fix(warnings: dict[str, list[str]], first_round: bool) -> bool:
    """Round 1 fixes anything flagged; later rounds only length / thread problems and AI tells."""
    flagged = [w for ws in warnings.values() for w in ws]
    if first_round:
        return bool(flagged)
    return any(is_hard_warning(w) or w.startswith("AI-ish") for w in flagged)


def produce_drafts(cfg: Config, llm: LLM, session: Session, topic: Topic, http_client: httpx.Client | None = None) -> list[Draft]:
    """Write (with the author's real writing as examples) → de-AI polish → fix only what checks flag."""
    items = items_for_topic(session, topic)
    fetch_fulltext(cfg, session, items, http_client)
    style = load_style(cfg)
    voice_text = voice_for(cfg, f"{topic.title} {topic.angle} {topic.user_take} {topic.my_connection}")
    now = utcnow()
    written = write_drafts(llm, cfg, topic, items, style, voice_text)
    versions: dict[str, PostVersion] = {lang: getattr(written, lang) for lang in LANGS}
    revisions = {lang: [{"stage": "written", "tweets": versions[lang].tweets}] for lang in LANGS}
    stage, alt_hooks, stage_errors = "written", {lang: [] for lang in LANGS}, []

    def checks_for(vs: dict[str, PostVersion]) -> dict[str, list[str]]:
        return {lang: run_checks(cfg, session, Draft(topic_id=topic.id, lang=lang, tweets=v.tweets,
                                                     source_urls=v.source_urls, created_at=now, updated_at=now), items)
                for lang, v in vs.items()}

    def apply(result: OptimizedDraft, stage_name: str) -> None:
        for lang in LANGS:
            ov = getattr(result, lang)
            versions[lang] = PostVersion(tweets=ov.tweets, source_urls=ov.source_urls or versions[lang].source_urls)
            alt_hooks[lang] = ov.alt_hooks[:3] or alt_hooks[lang]
            revisions[lang].append({"stage": stage_name, "tweets": ov.tweets, "notes": ov.notes})

    try:
        apply(polish_drafts(llm, cfg, topic, versions, items, style, voice_text), "polished")
        stage = "polished"
    except LLMError as e:
        stage_errors.append(f"Polish step failed: {e}")

    if stage == "polished":
        for round_no in range(1 + max(0, cfg.pipeline.optimize_rounds)):
            warnings = checks_for(versions)
            if not _needs_fix(warnings, round_no == 0):
                stage = "optimized"  # checks are clean: nothing to fix
                break
            try:
                apply(optimize_drafts(llm, cfg, topic, versions, warnings, items, style, voice_text), "optimized")
            except LLMError as e:
                stage_errors.append(f"Optimize step failed: {e}")
                break
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
        lang_name=LANG_NAMES.get(draft.lang, draft.lang), style=style, thread_max=cfg.draft.thread_max_tweets,
        voice=voice_for(cfg, " ".join(draft.tweets)), tells=prompts.AI_TELLS,
    )
    current = "\n---\n".join(draft.tweets)
    user = (
        f"Instruction: {instruction}\n\nCurrent post (tweets separated by ---):\n{current}\n\n"
        f"Current source URLs: {', '.join(draft.source_urls) or '(none)'}\n\n"
        + _material(cfg, items)
    )
    return llm.parse(system, user, PostVersion, temperature=cfg.llm.temperature_edit)
