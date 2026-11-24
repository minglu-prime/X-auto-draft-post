"""Local review dashboard. Binds to 127.0.0.1; state-changing requests need the app's own header."""

from __future__ import annotations

import logging
import re
from html import escape as html_escape
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import Session, col, select

from xdraft.config import Config
from xdraft import media, voice
from xdraft.db import get_engine
from xdraft.llm import LLM, LLMError, make_llm
from xdraft.models import Draft, DraftStatus, Item, Topic, TopicStatus
from xdraft.pipeline.check import MAX_WEIGHTED, weighted_length
from xdraft.pipeline.generate import (
    items_for_topic,
    load_style,
    produce_drafts,
    rewrite_post,
    run_checks,
    suggest_topics,
    trend_snapshot,
)
from xdraft.publish import service
from xdraft.publish.x import PendingAuth, XClient, XError
from xdraft.runner import run_collect
from xdraft.timeutil import to_utc, utcnow

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
CSRF_HEADER = "x-xdraft"
REWRITE_PRESETS = {
    "shorter": "Make it shorter and tighter. Keep the key insight.",
    "punchier": "Make the first line a stronger hook. Keep it honest, no hype.",
    "personal": "Make it more personal: more of what I did and saw, less summary.",
    "single": "Turn it into a single tweet.",
    "regenerate": "Write a fresh version with a different structure.",
}
PRESET_ZH = {"shorter": "更短更紧凑", "punchier": "开头更抓人", "personal": "更有个人经历",
             "single": "改成单条推文", "regenerate": "换个结构重写"}
STATUS_ZH = {"pending": "待审", "scheduled": "已排期", "due": "该发了", "failed": "发布失败", "published": "已发布",
             "dry_run": "未发出", "rejected": "已跳过", "suggested": "待选", "drafted": "已写稿", "dismissed": "已跳过"}
STAGE_ZH = {"written": "初稿", "polished": "润色", "optimized": "优化"}
WEEKDAY_ZH = "一二三四五六日"
STALE_MATERIAL = timedelta(hours=2)  # suggesting topics refreshes material older than this first

# Display-only Chinese for pipeline/check.py warnings (stored warnings stay English: generate.py matches them).
_WARN_ZH = [
    (re.compile(r'^AI-ish: "(\d+) em dashes"$'), "像 AI 写的：用了 {0} 个破折号"),
    (re.compile(r'^AI-ish: "(.*)"$', re.DOTALL), "像 AI 写的：「{0}」"),
    (re.compile(r"^Thread has (\d+) tweets \(max (\d+)\)$"), "串推有 {0} 条，上限 {1} 条"),
    (re.compile(r"^Tweet (\d+) is (\d+)/(\d+) weighted characters$"), "第 {0} 条 {1}/{2}，超长了"),
    (re.compile(r"^Tweet (\d+) is empty$"), "第 {0} 条是空的"),
    (re.compile(r"^Link not found in the source material: (.*)$"), "素材里没有这个链接：{0}"),
    (re.compile(r"^Number “(.*)” does not appear in the sources — verify it$"), "数字「{0}」在素材里找不到，核对一下"),
    (re.compile(r"^Very similar to a recent post \((\d+%) match\)$"), "和最近一篇很像（相似度 {0}）"),
    (re.compile(r"^Possible secret, email or personal path in the text$"), "可能含密钥、邮箱或个人路径"),
    (re.compile(r"^Draft is empty$"), "草稿是空的"),
]


def warn_zh(warning: str) -> str:
    for pattern, zh in _WARN_ZH:
        m = pattern.match(warning)
        if m:
            return zh.format(*m.groups())
    return warning


def current_step(n_items: int, n_topics: int, n_pending: int, n_scheduled: int, n_due: int = 0) -> str:
    """Which workflow step the dashboard should point the user at next."""
    if n_due:
        return "publish"  # time-sensitive: a post is waiting to be posted by hand
    if n_items == 0:
        return "collect"
    if n_pending:
        return "review"
    if n_topics or not n_scheduled:
        return "pick"
    return "publish"


def split_tweets(body: str) -> list[str]:
    parts = re.split(r"(?m)^\s*-{3,}\s*$", body.replace("\r\n", "\n"))
    return [p.strip() for p in parts if p.strip()]


def create_app(
    cfg: Config,
    llm_factory: Callable[[], LLM] | None = None,
    x_client_factory: Callable[[], XClient] | None = None,
    start_scheduler: bool = True,
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost"),
) -> FastAPI:
    engine = get_engine(cfg.resolve_path(cfg.db_path))
    with Session(engine) as s:  # older versions marked unposted drafts "dry_run"; they still need posting
        for d in s.exec(select(Draft).where(Draft.status == DraftStatus.DRY_RUN)).all():
            d.status = DraftStatus.DUE
            s.add(d)
        s.commit()
    llm_factory = llm_factory or (lambda: make_llm(cfg.llm))
    x_client_factory = x_client_factory or (lambda: service.make_x_client(cfg))
    pending_auth: dict[str, PendingAuth] = {}

    def publish_job() -> None:
        with Session(engine) as s:
            for d in service.publish_due(cfg, s, x_client_factory):
                log.info("draft #%s -> %s", d.id, d.status)

    def suggest_job() -> None:
        since = utcnow() - timedelta(days=cfg.lookback_days)
        try:
            run_collect(cfg, since)
            with Session(engine) as s:
                topics = suggest_topics(cfg, llm_factory(), s, since)
            log.info("daily run: %d topic suggestions", len(topics))
        except Exception as e:  # noqa: BLE001 - keep the scheduler alive
            log.warning("daily suggest failed: %s", e)

    def metrics_job() -> None:
        try:
            with Session(engine) as s:
                service.refresh_metrics(cfg, s, x_client_factory)
        except XError as e:
            log.warning("metrics refresh failed: %s", e)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        scheduler = None
        if start_scheduler:
            from apscheduler.schedulers.background import BackgroundScheduler

            scheduler = BackgroundScheduler(timezone="UTC")
            scheduler.add_job(publish_job, "interval", seconds=60, id="publish", max_instances=1, coalesce=True)
            if cfg.pipeline.auto_suggest_at:
                hh, mm = (int(x) for x in cfg.pipeline.auto_suggest_at.split(":"))
                scheduler.add_job(suggest_job, "cron", hour=hh, minute=mm, timezone=cfg.timezone,
                                  id="suggest", max_instances=1, coalesce=True)
            if cfg.publish.enabled:
                scheduler.add_job(metrics_job, "interval", minutes=cfg.publish.metrics_every_minutes,
                                  id="metrics", max_instances=1, coalesce=True)
            scheduler.start()
        yield
        if scheduler:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="xdraft", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    tz = ZoneInfo(cfg.timezone)

    flash: list[str] = []  # one-shot HTML messages shown on the next page load (single local user)

    def local(dt: datetime | None, fmt: str = "%m-%d %H:%M") -> str:
        return to_utc(dt).astimezone(tz).strftime(fmt) if dt else ""

    def local_day(dt: datetime | None) -> str:
        """e.g. 周三 01-02 09:30, in the configured timezone."""
        if not dt:
            return ""
        d = to_utc(dt).astimezone(tz)
        return f"周{WEEKDAY_ZH[d.weekday()]} {d:%m-%d %H:%M}"

    def next_slot_label() -> str:
        with Session(engine) as s:
            slot = service.next_free_slot(cfg, s)
        return local_day(slot)

    def take_flash() -> list[str]:
        messages = flash[:]
        flash.clear()
        return messages

    templates.env.globals.update(weighted_length=weighted_length, MAX_WEIGHTED=MAX_WEIGHTED,
                                 presets=REWRITE_PRESETS, preset_zh=PRESET_ZH, status_zh=STATUS_ZH,
                                 stage_zh=STAGE_ZH, cfg=cfg, next_slot_label=next_slot_label,
                                 take_flash=take_flash, images_for=media.images_for,
                                 # cache-buster so browsers pick up new CSS/JS after an update
                                 asset_v=lambda: int(max(p.stat().st_mtime for p in (HERE / "static").iterdir())))
    templates.env.filters["local"] = local
    templates.env.filters["local_day"] = local_day

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in allowed_hosts:  # blocks DNS-rebinding pages from reading or acting
            return PlainTextResponse("Forbidden host", status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != "1":
            # Cross-site forms cannot set custom headers, so this stops CSRF from other websites.
            return PlainTextResponse("Missing request header", status_code=403)
        return await call_next(request)

    def get_draft(s: Session, draft_id: int) -> Draft:
        d = s.get(Draft, draft_id)
        if not d:
            raise HTTPException(404, "draft not found")
        return d

    def card(request: Request, s: Session, d: Draft, message: str = "", error: str = "",
             undo: str = "") -> HTMLResponse:
        """A draft card. `message` becomes a toast (with an undo button when `undo` is a POST path)."""
        topic = s.get(Topic, d.topic_id) if d.topic_id else None
        return templates.TemplateResponse(request, "_draft.html", {
            "d": d, "topic": topic, "message": message, "error": error, "undo": undo,
        })

    def gone(request: Request, el_id: str, message: str, undo: str) -> HTMLResponse:
        """Hidden placeholder that takes a removed card's place, so undo can put the card back there."""
        return templates.TemplateResponse(request, "_gone.html", {"el_id": el_id, "message": message, "undo": undo})

    def x_status() -> dict:
        client = x_client_factory()
        connected = client.connected
        return {"connected": connected, "client_id_set": bool(client.client_id),
                "auto": service.auto_posting(cfg, client)}

    templates.env.globals["x_state"] = x_status
    templates.env.filters["warn_zh"] = warn_zh

    # ---- pages -----------------------------------------------------------------
    def group_by_topic(s: Session, drafts) -> list[dict]:
        groups: dict[int | None, dict] = {}
        for d in drafts:
            g = groups.setdefault(d.topic_id, {"topic": s.get(Topic, d.topic_id) if d.topic_id else None,
                                               "drafts": []})
            g["drafts"].append(d)
        for g in groups.values():
            g["items"] = items_for_topic(s, g["topic"]) if g["topic"] else []
        return list(groups.values())

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with Session(engine) as s:
            drafts = s.exec(select(Draft).where(
                col(Draft.status).in_([DraftStatus.PENDING, DraftStatus.SCHEDULED, DraftStatus.DUE,
                                       DraftStatus.FAILED])
            ).order_by(col(Draft.topic_id).desc(), Draft.lang.desc())).all()
            review = [d for d in drafts if d.status in (DraftStatus.PENDING, DraftStatus.FAILED)]
            def by_time(d: Draft) -> datetime:
                return to_utc(d.scheduled_at) if d.scheduled_at else utcnow()
            due = sorted((d for d in drafts if d.status == DraftStatus.DUE), key=by_time)
            scheduled = sorted((d for d in drafts if d.status == DraftStatus.SCHEDULED), key=by_time)
            topics = s.exec(select(Topic).where(Topic.status == TopicStatus.SUGGESTED)
                            .order_by(col(Topic.created_at).desc()).limit(40)).all()
            topics = sorted(topics, key=lambda t: -t.score)  # best first; the page folds the tail
            n_items = s.exec(select(func.count()).select_from(Item)).one()
            return templates.TemplateResponse(request, "index.html", {
                "review_groups": group_by_topic(s, review), "scheduled": scheduled, "due": due,
                "topics": topics, "items_by_topic": {t.id: items_for_topic(s, t) for t in topics},
                "snap": trend_snapshot(cfg, s, utcnow() - timedelta(days=cfg.lookback_days)),
                "n_items": n_items, "n_review": len(review),
                "step": current_step(n_items, len(topics), len(review), len(scheduled), len(due)),
                "x": x_status(),
            })

    @app.get("/status")
    def status():
        """Counts the open dashboard polls, so it can say "a post is due" without a reload."""
        with Session(engine) as s:
            def count(*statuses: str) -> int:
                return s.exec(select(func.count()).select_from(Draft)
                              .where(col(Draft.status).in_(statuses))).one()
            return {"due": count(DraftStatus.DUE), "review": count(DraftStatus.PENDING, DraftStatus.FAILED),
                    "scheduled": count(DraftStatus.SCHEDULED)}

    @app.get("/topics")
    def topics_page():
        return RedirectResponse("/#topics", status_code=307)

    def topic_card(request: Request, s: Session, t: Topic, message: str = "", error: str = "") -> HTMLResponse:
        return templates.TemplateResponse(request, "_topic.html", {
            "t": t, "topic_items": items_for_topic(s, t), "message": message, "error": error,
        })

    @app.post("/suggest")
    def suggest(days: int = Form(0), count: int = Form(0)):
        since = utcnow() - timedelta(days=days or cfg.lookback_days)
        with Session(engine) as s:
            last = s.exec(select(func.max(Item.collected_at))).one()
        if last is None or to_utc(last) < utcnow() - STALE_MATERIAL:  # don't make the user collect first
            run_collect(cfg, since)
        try:
            with Session(engine) as s:
                created = suggest_topics(cfg, llm_factory(), s, since, count or None)
        except LLMError as e:
            return HTMLResponse(f"<p class='warn'>生成选题失败：{html_escape(str(e))}</p>")
        if not created:
            return HTMLResponse("<p class='warn'>这次没找到值得写的选题，过几小时素材多了再试。</p>")
        flash.append(f"<p class='ok'>已生成 {len(created)} 个选题</p>")
        return Response(status_code=204, headers={"X-Redirect": "/#topics"})

    @app.post("/topics/{topic_id}/write", response_class=HTMLResponse)
    def write_topic(request: Request, topic_id: int, take: str = Form(""), format: str = Form("")):
        with Session(engine) as s:
            t = s.get(Topic, topic_id)
            if not t:
                raise HTTPException(404, "topic not found")
            if t.status != TopicStatus.SUGGESTED:
                return topic_card(request, s, t, error=f"这个选题已处理过（{STATUS_ZH.get(t.status, t.status)}）")
            if take.strip():
                t.user_take = take.strip()
            if format in ("thread", "quick"):
                t.format = format
            s.add(t)
            s.commit()
            try:
                drafts = produce_drafts(cfg, llm_factory(), s, t)
            except LLMError as e:
                return topic_card(request, s, t, error=f"写稿失败：{e}")
            flash.append(f"<p class='ok'>《{html_escape(t.title)}》中英草稿已生成（{len(drafts)} 篇）</p>")
            return Response(status_code=204, headers={"X-Redirect": "/#drafts"})

    @app.post("/topics/{topic_id}/dismiss", response_class=HTMLResponse)
    def dismiss_topic(request: Request, topic_id: int):
        with Session(engine) as s:
            t = s.get(Topic, topic_id)
            if not t:
                raise HTTPException(404, "topic not found")
            if t.status == TopicStatus.SUGGESTED:
                t.status = TopicStatus.DISMISSED
                s.add(t)
                s.commit()
            return gone(request, f"topic-{t.id}", f"已跳过「{t.title}」", f"/topics/{t.id}/restore")

    @app.post("/topics/{topic_id}/restore", response_class=HTMLResponse)
    def restore_topic(request: Request, topic_id: int):
        with Session(engine) as s:
            t = s.get(Topic, topic_id)
            if not t:
                raise HTTPException(404, "topic not found")
            if t.status == TopicStatus.DISMISSED:
                t.status = TopicStatus.SUGGESTED
                s.add(t)
                s.commit()
                s.refresh(t)
            return topic_card(request, s, t, message="已撤销")

    @app.post("/drafts/{draft_id}/hook", response_class=HTMLResponse)
    def use_hook(request: Request, draft_id: int, index: int = Form(...)):
        with Session(engine) as s:
            d = get_draft(s, draft_id)
            if not (0 <= index < len(d.alt_hooks)) or not d.tweets:
                return card(request, s, d, error="找不到这个开头")
            first, _, rest = d.tweets[0].partition("\n")
            new_hook = d.alt_hooks[index]
            d.alt_hooks = [h for i, h in enumerate(d.alt_hooks) if i != index] + [first]
            d.tweets = [new_hook + ("\n" + rest if rest else "")] + d.tweets[1:]
            d.edited, d.updated_at = True, utcnow()
            topic = s.get(Topic, d.topic_id) if d.topic_id else None
            d.warnings = run_checks(cfg, s, d, items_for_topic(s, topic) if topic else [])
            s.add(d)
            s.commit()
            s.refresh(d)
            return card(request, s, d, message="已换开头，原来的留作备选")

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request):
        with Session(engine) as s:
            published = s.exec(select(Draft).where(Draft.status == DraftStatus.PUBLISHED)
                               .order_by(col(Draft.published_at).desc()).limit(200)).all()
            skipped = s.exec(select(Draft).where(Draft.status == DraftStatus.REJECTED)
                             .order_by(col(Draft.updated_at).desc()).limit(100)).all()
            return templates.TemplateResponse(request, "history.html", {
                "drafts": published, "skipped": skipped, "x": x_status(),
                "can_refresh": any(d.tweet_ids for d in published)})

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request):
        with Session(engine) as s:
            stats = s.exec(select(Item.source, func.count(), func.max(Item.ts)).group_by(Item.source)).all()
            items = s.exec(select(Item).order_by(col(Item.ts).desc()).limit(40)).all()
            return templates.TemplateResponse(request, "sources.html", {"stats": stats, "items": items, "x": x_status()})

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request):
        return templates.TemplateResponse(request, "inbox.html", {"x": x_status(), "voice": voice.summary(cfg)})

    @app.post("/voice", response_class=HTMLResponse)
    def add_voice(kind: str = Form("mine"), text: str = Form(""), url: str = Form("")):
        kind = "reference" if kind == "reference" else "mine"
        try:
            if url.strip():
                path = voice.save_url(cfg, url.strip())
            elif text.strip():
                path = voice.save_text(cfg, kind, text)
            else:
                return HTMLResponse("<p class='warn'>请粘贴文字或填一个链接。</p>")
        except Exception as e:  # noqa: BLE001 - shown to the user
            return HTMLResponse(f"<p class='warn'>保存失败：{html_escape(str(e))}</p>")
        info = voice.summary(cfg)
        return HTMLResponse(f"<p class='ok'>文风已更新（{html_escape(path.name)}）：现在有 {info['own']} 篇你自己的文字，"
                            f"{info['references']} 段参考摘录</p>")

    # ---- draft actions -----------------------------------------------------------
    @app.post("/drafts/{draft_id}/save", response_class=HTMLResponse)
    def save(request: Request, draft_id: int, body: str = Form(""),
             tweet: Annotated[list[str] | None, Form()] = None, src: Annotated[list[int] | None, Form()] = None):
        """Save edited text, either one box per tweet (`tweet` fields, with `src` = each box's previous
        position so images follow their tweet) or one `body` split on `---` lines.

        Autosave (X-Autosave header) gets only the warnings fragment back, so the editor being typed in
        is never replaced."""
        autosave = request.headers.get("x-autosave") == "1"
        def refuse(why: str) -> HTMLResponse:
            if autosave:
                return templates.TemplateResponse(request, "_draft_meta.html", {"d": d, "meta_error": why})
            return card(request, s, d, error=why)

        with Session(engine) as s:
            d = get_draft(s, draft_id)
            # Once posting started, the text must match what is on X: no more edits.
            if d.tweet_ids:
                return refuse(f"这篇已经发出 {len(d.tweet_ids)} 条，不能再改")
            if d.status not in (DraftStatus.PENDING, DraftStatus.FAILED, DraftStatus.SCHEDULED):
                return refuse(f"这篇现在是「{STATUS_ZH.get(d.status, d.status)}」，不能再改；刷新页面看看")
            if tweet is not None:
                old_images = media.images_for(d)
                pairs = [(t.strip(), (src or [])[i] if i < len(src or []) else -1) for i, t in enumerate(tweet)]
                emptied = [n for n, (t, i) in enumerate(pairs, 1) if not t and 0 <= i < len(old_images)
                           and old_images[i]]
                if emptied:  # dropping that tweet would silently drop its images
                    return refuse(f"第 {emptied[0]} 条有配图，先移除图片再清空这一条")
                pairs = [(t, i) for t, i in pairs if t]
                tweets = [t for t, _ in pairs]
            else:
                tweets, pairs = split_tweets(body), None
            if not tweets:
                if autosave:  # every box emptied mid-edit: keep the last saved text, say why
                    return templates.TemplateResponse(request, "_draft_meta.html", {
                        "d": d, "meta_error": "正文是空的，没有保存"})
                return card(request, s, d, error="草稿至少要有一条推文")
            old_images = media.images_for(d)
            if pairs is not None:  # boxes were added, removed or reordered: images move with their tweet
                images = [list(old_images[i]) if 0 <= i < len(old_images) else [] for _, i in pairs]
            else:
                images = None
            if tweets != d.tweets or (images is not None and images != old_images):
                d.tweets, d.edited, d.updated_at = tweets, True, utcnow()
                d.images = images if images is not None else media.images_for(d)
                topic = s.get(Topic, d.topic_id) if d.topic_id else None
                d.warnings = run_checks(cfg, s, d, items_for_topic(s, topic) if topic else [])
                s.add(d)
                s.commit()
                s.refresh(d)
            if autosave:
                return templates.TemplateResponse(request, "_draft_meta.html", {"d": d})
            return card(request, s, d, message="已保存")

    @app.post("/drafts/{draft_id}/images", response_class=HTMLResponse)
    async def add_image(request: Request, draft_id: int, file: Annotated[UploadFile, File()],
                        tweet: int = Form(1)):
        data = await file.read(media.MAX_BYTES + 1)
        with Session(engine) as s:
            d = get_draft(s, draft_id)
            try:
                media.save_image(cfg, d, tweet - 1, data)
            except ValueError as e:
                return card(request, s, d, error=str(e))
            d.updated_at = utcnow()
            s.add(d)
            s.commit()
            s.refresh(d)
            return card(request, s, d, message=f"已添加配图到第 {tweet} 条")

    @app.post("/drafts/{draft_id}/images/remove", response_class=HTMLResponse)
    def remove_image(request: Request, draft_id: int, tweet: int = Form(...), name: str = Form(...)):
        with Session(engine) as s:
            d = get_draft(s, draft_id)
            try:
                media.remove_image(cfg, d, tweet - 1, name)
            except ValueError as e:
                return card(request, s, d, error=str(e))
            d.updated_at = utcnow()
            s.add(d)
            s.commit()
            s.refresh(d)
            return card(request, s, d, message="已删除配图",
                        undo=f"/drafts/{d.id}/images/restore?{urlencode({'tweet': tweet, 'name': name})}")

    @app.post("/drafts/{draft_id}/images/restore", response_class=HTMLResponse)
    def restore_image(request: Request, draft_id: int, tweet: int, name: str):
        with Session(engine) as s:
            d = get_draft(s, draft_id)
            try:
                media.restore_image(cfg, d, tweet - 1, name)
            except ValueError as e:
                return card(request, s, d, error=str(e))
            d.updated_at = utcnow()
            s.add(d)
            s.commit()
            s.refresh(d)
            return card(request, s, d, message="已撤销")

    @app.get("/media/{draft_id}/{name}")
    def serve_image(draft_id: int, name: str):
        with Session(engine) as s:
            d = s.get(Draft, draft_id)
            path = media.image_path(cfg, d, name) if d else None
        if path is None or not path.is_file():
            raise HTTPException(404, "image not found")
        return FileResponse(path, media_type=media.mime_for(name))

    @app.post("/drafts/{draft_id}/rewrite", response_class=HTMLResponse)
    def rewrite(request: Request, draft_id: int, preset: str = Form(""), instruction: str = Form("")):
        with Session(engine) as s:
            d = get_draft(s, draft_id)
            text = instruction.strip() or REWRITE_PRESETS.get(preset, "")
            if not text:
                return card(request, s, d, error="请选一种改写方式，或者输入你的要求")
            topic = s.get(Topic, d.topic_id) if d.topic_id else None
            items = items_for_topic(s, topic) if topic else []
            try:
                version = rewrite_post(llm_factory(), cfg, d, items, text, load_style(cfg))
            except LLMError as e:
                return card(request, s, d, error=f"改写失败：{e}")
            d.tweets = [t.strip() for t in version.tweets if t.strip()]
            d.images = media.images_for(d)
            d.source_urls = version.source_urls or d.source_urls
            d.edited, d.updated_at = True, utcnow()
            d.warnings = run_checks(cfg, s, d, items)
            s.add(d)
            s.commit()
            s.refresh(d)
            return card(request, s, d, message="已改写")

    @app.post("/drafts/{draft_id}/approve", response_class=HTMLResponse)
    def approve(request: Request, draft_id: int, mode: str = Form("slot"), when: str = Form("")):
        with Session(engine) as s:
            d = get_draft(s, draft_id)
            at = None
            if mode == "custom":
                try:
                    local_dt = datetime.fromisoformat(when)
                except ValueError:
                    return card(request, s, d, error="日期时间格式不对")
                # The time is read in the configured timezone (shown next to the field), like the slots,
                # so it means the same thing whatever the browser's timezone is.
                at = to_utc(local_dt.replace(tzinfo=tz)) if local_dt.tzinfo is None else to_utc(local_dt)
            elif mode == "slot":
                at = service.next_free_slot(cfg, s)
                if at is None:
                    return card(request, s, d, error="未来 14 天没有空闲时段，请在 config.yaml 的 publish.slots 里加时段")
            try:
                service.approve(s, d, at)
            except ValueError as e:
                return card(request, s, d, error=str(e))
            if mode == "now":  # publish right away instead of waiting for the scheduler
                service.publish_draft(cfg, s, d, x_client_factory)
                msg = {DraftStatus.DUE: "可以发了：逐条复制到 X，发完点「我已发出」",
                       DraftStatus.PUBLISHED: "已发到 X"}.get(d.status, "")
                return card(request, s, d, message=msg, error=d.publish_error or "")
            how = "到点自动发到 X" if x_status()["auto"] else "到点提醒你发"
            return card(request, s, d, message=f"已排期：{local_day(d.scheduled_at)}，{how}",
                        undo=f"/drafts/{d.id}/unschedule")

    @app.post("/drafts/{draft_id}/unschedule", response_class=HTMLResponse)
    def unschedule(request: Request, draft_id: int):
        with Session(engine) as s:
            d = service.unschedule(s, get_draft(s, draft_id))
            return card(request, s, d, message="已撤回到待审")

    @app.post("/drafts/{draft_id}/reject", response_class=HTMLResponse)
    def reject(request: Request, draft_id: int):
        with Session(engine) as s:
            d = service.reject(s, get_draft(s, draft_id))
            return gone(request, f"draft-{d.id}", "已跳过这篇", f"/drafts/{d.id}/restore")

    @app.post("/drafts/{draft_id}/restore", response_class=HTMLResponse)
    def restore(request: Request, draft_id: int):
        with Session(engine) as s:
            d = service.restore(s, get_draft(s, draft_id))
            return card(request, s, d, message="已放回待审")

    @app.post("/drafts/{draft_id}/posted", response_class=HTMLResponse)
    def posted(request: Request, draft_id: int, url: str = Form("")):
        with Session(engine) as s:
            d = get_draft(s, draft_id)
            try:
                service.mark_posted(s, d, url)
            except ValueError as e:
                return card(request, s, d, error=str(e))
            return card(request, s, d, message="已记到「已发布」", undo=f"/drafts/{d.id}/unposted")

    @app.post("/drafts/{draft_id}/unposted", response_class=HTMLResponse)
    def unposted(request: Request, draft_id: int):
        with Session(engine) as s:
            d = service.unmark_posted(s, get_draft(s, draft_id))
            return card(request, s, d, message="已撤销")

    # ---- inbox / collect / generate ----------------------------------------------
    @app.post("/inbox", response_class=HTMLResponse)
    def add_note(title: str = Form(""), body: str = Form(...), tags: str = Form(""), url: str = Form("")):
        inbox_dir = cfg.resolve_path(cfg.notes.inbox_dir)
        inbox_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone()
        slug = re.sub(r"[^\w\-]+", "-", (title or "note").lower()).strip("-")[:40] or "note"
        path = inbox_dir / f"{now:%Y%m%d-%H%M%S}-{slug}.md"
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        fm = [f"date: {now.isoformat(timespec='seconds')}"]
        if tag_list:
            fm.append("tags: [" + ", ".join(tag_list) + "]")
        if url.strip():
            fm.append(f"url: {url.strip()}")
        text = "---\n" + "\n".join(fm) + "\n---\n" + (f"# {title.strip()}\n" if title.strip() else "") + body.strip() + "\n"
        path.write_text(text, encoding="utf-8")
        return HTMLResponse(f'<p class="ok">笔记已保存（{html_escape(path.name)}），下次收集会用到</p>')

    @app.post("/collect", response_class=HTMLResponse)
    def collect(days: int = Form(0), back: str = Form("/")):
        since = utcnow() - timedelta(days=days or cfg.lookback_days)
        reports = run_collect(cfg, since)
        new = sum(r.counts.new for r in reports if not r.skipped)
        updated = sum(r.counts.updated for r in reports if not r.skipped)
        errors = [(r.source, e) for r in reports for e in r.errors]
        summary = f"已收集：新增 {new}，更新 {updated}"
        if errors:  # partial failure is not a success: say so, keep the reasons visible
            failed = len({src for src, _ in errors})
            errs = "".join(f"<li>{html_escape(src)}：{html_escape(str(e))}</li>" for src, e in errors)
            flash.append(f"<p class='warn'>{summary}；{failed} 个来源失败</p><ul class='warn'>{errs}</ul>")
        else:
            flash.append(f"<p class='ok'>{summary}</p>")
        local_path = back.startswith("/") and not back.startswith("//")  # no open redirects
        return Response(status_code=204, headers={"X-Redirect": back if local_path else "/"})

    # ---- X account -------------------------------------------------------------
    @app.get("/x/login")
    def x_login():
        try:
            url, pending = x_client_factory().start_login()
        except XError as e:
            return PlainTextResponse(str(e), status_code=400)
        pending_auth.clear()
        pending_auth[pending.state] = pending
        return RedirectResponse(url)

    @app.get("/x/callback")
    def x_callback(state: str = "", code: str = "", error: str = ""):
        pending = pending_auth.pop(state, None)
        if error or not code or not pending:
            return PlainTextResponse(f"X 登录失败：{error or 'state 无效或已过期，请重新连接'}", status_code=400)
        try:
            x_client_factory().finish_login(code, pending)
        except XError as e:
            return PlainTextResponse(str(e), status_code=400)
        return RedirectResponse("/", status_code=303)

    @app.post("/x/logout")
    def x_logout():
        x_client_factory().store.clear()
        return Response(status_code=204, headers={"X-Redirect": "/"})

    @app.post("/metrics/refresh", response_class=HTMLResponse)
    def metrics_refresh():
        try:
            with Session(engine) as s:
                n = service.refresh_metrics(cfg, s, x_client_factory)
        except XError as e:
            return HTMLResponse(f"<p class='warn'>刷新互动数据失败：{html_escape(str(e))}</p>")
        return Response(status_code=204, headers={"X-Redirect": "/history"}) if n else HTMLResponse(
            "<p>没有已发布的帖子需要更新。</p>")

    return app
