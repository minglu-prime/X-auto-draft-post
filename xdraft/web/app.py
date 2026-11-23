"""Local review dashboard. Binds to 127.0.0.1; state-changing requests need the app's own header."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import Session, col, select

from xdraft.config import Config
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

    def local(dt: datetime | None, fmt: str = "%m-%d %H:%M") -> str:
        return to_utc(dt).astimezone(tz).strftime(fmt) if dt else ""

    def next_slot_label() -> str:
        with Session(engine) as s:
            slot = service.next_free_slot(cfg, s)
        return local(slot, "%a %m-%d %H:%M") if slot else ""

    templates.env.globals.update(weighted_length=weighted_length, MAX_WEIGHTED=MAX_WEIGHTED,
                                 presets=REWRITE_PRESETS, cfg=cfg, next_slot_label=next_slot_label)
    templates.env.filters["local"] = local

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

    def card(request: Request, s: Session, d: Draft, message: str = "", error: str = "") -> HTMLResponse:
        topic = s.get(Topic, d.topic_id) if d.topic_id else None
        return templates.TemplateResponse(request, "_draft.html", {
            "d": d, "topic": topic, "message": message, "error": error,
        })

    def x_status() -> dict:
        client = x_client_factory()
        return {"connected": client.connected, "client_id_set": bool(client.client_id)}

    # ---- pages -----------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with Session(engine) as s:
            drafts = s.exec(select(Draft).where(
                col(Draft.status).in_([DraftStatus.PENDING, DraftStatus.SCHEDULED, DraftStatus.FAILED])
            ).order_by(col(Draft.topic_id).desc(), Draft.lang.desc())).all()
            groups: dict[int | None, dict] = {}
            for d in drafts:
                g = groups.setdefault(d.topic_id, {"topic": s.get(Topic, d.topic_id) if d.topic_id else None,
                                                   "drafts": [], "items": []})
                g["drafts"].append(d)
            for g in groups.values():
                g["items"] = items_for_topic(s, g["topic"]) if g["topic"] else []
            n_items = s.exec(select(func.count()).select_from(Item)).one()
            return templates.TemplateResponse(request, "index.html", {
                "groups": list(groups.values()), "n_items": n_items, "x": x_status(),
            })

    @app.get("/topics", response_class=HTMLResponse)
    def topics_page(request: Request):
        with Session(engine) as s:
            topics = s.exec(select(Topic).where(Topic.status == TopicStatus.SUGGESTED)
                            .order_by(col(Topic.created_at).desc(), col(Topic.score).desc()).limit(40)).all()
            items = {t.id: items_for_topic(s, t) for t in topics}
            snap = trend_snapshot(cfg, s, utcnow() - timedelta(days=cfg.lookback_days))
            return templates.TemplateResponse(request, "topics.html", {
                "topics": topics, "items": items, "snap": snap, "x": x_status(),
            })

    def topic_card(request: Request, s: Session, t: Topic, message: str = "", error: str = "") -> HTMLResponse:
        return templates.TemplateResponse(request, "_topic.html", {
            "t": t, "topic_items": items_for_topic(s, t), "message": message, "error": error,
        })

    @app.post("/suggest")
    def suggest(days: int = Form(0), count: int = Form(0)):
        since = utcnow() - timedelta(days=days or cfg.lookback_days)
        try:
            with Session(engine) as s:
                created = suggest_topics(cfg, llm_factory(), s, since, count or None)
        except LLMError as e:
            return HTMLResponse(f"<p class='warn'>{e}</p>")
        if not created:
            return HTMLResponse("<p class='warn'>No suggestions: collect some material first.</p>")
        return Response(status_code=204, headers={"X-Redirect": "/topics"})

    @app.post("/topics/{topic_id}/write", response_class=HTMLResponse)
    def write_topic(request: Request, topic_id: int):
        with Session(engine) as s:
            t = s.get(Topic, topic_id)
            if not t:
                raise HTTPException(404, "topic not found")
            if t.status != TopicStatus.SUGGESTED:
                return topic_card(request, s, t, error=f"Already {t.status}")
            try:
                drafts = produce_drafts(cfg, llm_factory(), s, t)
            except LLMError as e:
                return topic_card(request, s, t, error=str(e))
            return topic_card(request, s, t, message=f"Written, polished and optimized: {len(drafts)} drafts are in the Queue")

    @app.post("/topics/{topic_id}/dismiss", response_class=HTMLResponse)
    def dismiss_topic(topic_id: int):
        with Session(engine) as s:
            t = s.get(Topic, topic_id)
            if t and t.status == TopicStatus.SUGGESTED:
                t.status = TopicStatus.DISMISSED
                s.add(t)
                s.commit()
        return HTMLResponse("")

    @app.post("/drafts/{draft_id}/hook", response_class=HTMLResponse)
    def use_hook(request: Request, draft_id: int, index: int = Form(...)):
        with Session(engine) as s:
            d = get_draft(s, draft_id)
            if not (0 <= index < len(d.alt_hooks)) or not d.tweets:
                return card(request, s, d, error="Unknown hook")
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
            return card(request, s, d, message="Hook swapped (the old one is kept as an alternative)")

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request):
        with Session(engine) as s:
            drafts = s.exec(select(Draft).where(
                col(Draft.status).in_([DraftStatus.PUBLISHED, DraftStatus.DRY_RUN, DraftStatus.REJECTED])
            ).order_by(col(Draft.updated_at).desc()).limit(200)).all()
            return templates.TemplateResponse(request, "history.html", {"drafts": drafts, "x": x_status()})

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request):
        with Session(engine) as s:
            stats = s.exec(select(Item.source, func.count(), func.max(Item.ts)).group_by(Item.source)).all()
            items = s.exec(select(Item).order_by(col(Item.ts).desc()).limit(40)).all()
            return templates.TemplateResponse(request, "sources.html", {"stats": stats, "items": items, "x": x_status()})

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request):
        return templates.TemplateResponse(request, "inbox.html", {"x": x_status()})

    # ---- draft actions -----------------------------------------------------------
    @app.post("/drafts/{draft_id}/save", response_class=HTMLResponse)
    def save(request: Request, draft_id: int, body: str = Form(...)):
        with Session(engine) as s:
            d = get_draft(s, draft_id)
            tweets = split_tweets(body)
            if not tweets:
                return card(request, s, d, error="A draft needs at least one tweet")
            d.tweets, d.edited, d.updated_at = tweets, True, utcnow()
            topic = s.get(Topic, d.topic_id) if d.topic_id else None
            d.warnings = run_checks(cfg, s, d, items_for_topic(s, topic) if topic else [])
            s.add(d)
            s.commit()
            s.refresh(d)
            return card(request, s, d, message="Saved")

    @app.post("/drafts/{draft_id}/rewrite", response_class=HTMLResponse)
    def rewrite(request: Request, draft_id: int, preset: str = Form(""), instruction: str = Form("")):
        with Session(engine) as s:
            d = get_draft(s, draft_id)
            text = instruction.strip() or REWRITE_PRESETS.get(preset, "")
            if not text:
                return card(request, s, d, error="Pick a rewrite option or type an instruction")
            topic = s.get(Topic, d.topic_id) if d.topic_id else None
            items = items_for_topic(s, topic) if topic else []
            try:
                version = rewrite_post(llm_factory(), cfg, d, items, text, load_style(cfg))
            except LLMError as e:
                return card(request, s, d, error=str(e))
            d.tweets = [t.strip() for t in version.tweets if t.strip()]
            d.source_urls = version.source_urls or d.source_urls
            d.edited, d.updated_at = True, utcnow()
            d.warnings = run_checks(cfg, s, d, items)
            s.add(d)
            s.commit()
            s.refresh(d)
            return card(request, s, d, message="Rewritten")

    @app.post("/drafts/{draft_id}/approve", response_class=HTMLResponse)
    def approve(request: Request, draft_id: int, mode: str = Form("slot"), when: str = Form(""),
                tz_offset: int = Form(0)):
        with Session(engine) as s:
            d = get_draft(s, draft_id)
            at = None
            if mode == "custom":
                try:
                    local_dt = datetime.fromisoformat(when)
                except ValueError:
                    return card(request, s, d, error="Invalid date/time")
                # Browser offset is minutes *behind* UTC (getTimezoneOffset).
                at = (local_dt + timedelta(minutes=tz_offset)).replace(tzinfo=timezone.utc)
            elif mode == "slot":
                at = service.next_free_slot(cfg, s)
                if at is None:
                    return card(request, s, d, error="No free slot in the next 14 days; add slots in config")
            try:
                service.approve(s, d, at)
            except ValueError as e:
                return card(request, s, d, error=str(e))
            if mode == "now":  # publish right away instead of waiting for the scheduler
                service.publish_draft(cfg, s, d, x_client_factory)
                msg = {DraftStatus.DRY_RUN: "Dry run: logged, not posted (publish.enabled is false)",
                       DraftStatus.PUBLISHED: "Posted to X"}.get(d.status, "")
                return card(request, s, d, message=msg, error=d.publish_error or "")
            return card(request, s, d, message=f"Scheduled for {local(d.scheduled_at, '%a %m-%d %H:%M')} ({cfg.timezone})")

    @app.post("/drafts/{draft_id}/unschedule", response_class=HTMLResponse)
    def unschedule(request: Request, draft_id: int):
        with Session(engine) as s:
            d = service.unschedule(s, get_draft(s, draft_id))
            return card(request, s, d, message="Back in the queue")

    @app.post("/drafts/{draft_id}/reject", response_class=HTMLResponse)
    def reject(request: Request, draft_id: int):
        with Session(engine) as s:
            d = service.reject(s, get_draft(s, draft_id))
            return card(request, s, d, message="Rejected")

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
        return HTMLResponse(f'<p class="ok">Saved {path.name}. It will be used in the next collect.</p>')

    @app.post("/collect", response_class=HTMLResponse)
    def collect(days: int = Form(0)):
        since = utcnow() - timedelta(days=days or cfg.lookback_days)
        reports = run_collect(cfg, since)
        rows = "".join(
            f"<li><b>{r.source}</b>: " + ("skipped" if r.skipped else
                f"{r.collected} found, {r.counts.new} new, {r.counts.updated} updated"
                + (f", <span class='warn'>{len(r.errors)} errors</span>" if r.errors else "")) + "</li>"
            for r in reports
        )
        errs = "".join(f"<li class='warn'>{r.source}: {e}</li>" for r in reports for e in r.errors)
        return HTMLResponse(f"<ul>{rows}{errs}</ul>")

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
            return PlainTextResponse(f"X login failed: {error or 'invalid or expired state'}", status_code=400)
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
            return HTMLResponse(f"<p class='warn'>{e}</p>")
        return Response(status_code=204, headers={"X-Redirect": "/history"}) if n else HTMLResponse(
            "<p>No published posts to update.</p>")

    return app
