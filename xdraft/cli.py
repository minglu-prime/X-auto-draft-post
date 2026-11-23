"""Command line for xdraft. Every command works the same in PowerShell, cmd and bash."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from sqlmodel import Session, select

from xdraft.collectors import COLLECTORS
from xdraft.config import load_config
from xdraft.db import get_engine
from xdraft.llm import LLMError, make_llm
from xdraft.models import Draft, DraftStatus, Item, Topic
from xdraft.runner import run_collect
from xdraft.timeutil import utcnow

app = typer.Typer(help="Turn your AI usage, model takes and AI news into X drafts.", no_args_is_help=True)
console = Console()

ConfigOpt = typer.Option(None, "--config", "-c", help="Path to config.yaml (default: ./config.yaml).")


@app.callback()
def main(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    log_file: Optional[Path] = typer.Option(None, "--log-file", help="Append output to this file (used by scheduled tasks)."),
) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    console.file = None  # back to stdout (matters when commands run in one process, e.g. tests)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        console.file = open(log_file, "a", encoding="utf-8")  # noqa: SIM115 - lives for the process
        console.print(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} xdraft {ctx.invoked_subcommand}", markup=False)
        logging.basicConfig(level=level, format=fmt, stream=console.file)
    else:
        if sys.stdout is None:  # pythonw.exe has no console
            console.file = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        logging.basicConfig(level=level, format=fmt)


def _check_sources(sources: list[str]) -> None:
    unknown = [s for s in sources if s not in COLLECTORS]
    if unknown:
        raise typer.BadParameter(f"unknown source(s) {unknown}; choose from {list(COLLECTORS)}")


@app.command()
def collect(
    source: list[str] = typer.Option([], "--source", "-s", help="Only run these collectors (repeatable)."),
    days: Optional[int] = typer.Option(None, "--days", "-d", help="Look back N days (default: lookback_days)."),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Collect material from all enabled sources into the local database."""
    _check_sources(source)
    cfg = load_config(config)
    since = utcnow() - timedelta(days=days if days is not None else cfg.lookback_days)
    reports = run_collect(cfg, since, source or None)

    table = Table(title=f"Collected since {since:%Y-%m-%d %H:%M} UTC")
    for col in ("source", "found", "new", "updated", "unchanged", "errors"):
        table.add_column(col, justify="left" if col == "source" else "right")
    for r in reports:
        if r.skipped:
            table.add_row(r.source, "[dim]skipped[/dim]", "", "", "", "")
            continue
        table.add_row(r.source, str(r.collected), str(r.counts.new), str(r.counts.updated),
                      str(r.counts.unchanged), str(len(r.errors)) if r.errors else "")
    console.print(table)
    for r in reports:
        for err in r.errors:
            console.print(f"[yellow]{r.source}[/yellow]: {err}")
    console.print(f"Database: {cfg.resolve_path(cfg.db_path)}")


@app.command()
def items(
    source: Optional[str] = typer.Option(None, "--source", "-s"),
    limit: int = typer.Option(20, "--limit", "-n"),
    full: bool = typer.Option(False, "--full", help="Print item bodies."),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """List collected items, newest first."""
    if source:
        _check_sources([source])
    cfg = load_config(config)
    engine = get_engine(cfg.resolve_path(cfg.db_path))
    with Session(engine) as session:
        q = select(Item).order_by(Item.ts.desc()).limit(limit)
        if source:
            q = q.where(Item.source == source)
        rows = session.exec(q).all()
    if not full:
        table = Table()
        for col in ("id", "source", "time (UTC)", "title"):
            table.add_column(col)
        for it in rows:
            table.add_row(str(it.id), it.source, f"{it.ts:%m-%d %H:%M}", it.title[:90])
        console.print(table)
        return
    for it in rows:
        console.rule(f"#{it.id} [{it.source}] {it.title}")
        console.print(f"{it.ts:%Y-%m-%d %H:%M} UTC" + (f"  {it.url}" if it.url else ""))
        console.print(it.body, markup=False, highlight=False)


def print_drafts(drafts: list[Draft], topics: dict[int, Topic]) -> None:
    for d in drafts:
        topic = topics.get(d.topic_id)
        console.rule(f"draft #{d.id} [{d.lang}] {d.status} — {topic.title if topic else ''}")
        for i, t in enumerate(d.tweets, 1):
            prefix = f"{i}/{len(d.tweets)} " if len(d.tweets) > 1 else ""
            console.print(prefix + t, markup=False, highlight=False)
        if d.source_urls:
            console.print("sources: " + ", ".join(d.source_urls), style="dim", markup=False)
        for w in d.warnings:
            console.print(f"⚠ {w}", style="yellow", markup=False)


def print_topics(topics: list[Topic]) -> None:
    for t in topics:
        console.rule(f"topic #{t.id} [{t.status}] score {t.score} — {t.title}")
        console.print(f"Angle: {t.angle}", markup=False, highlight=False)
        console.print(f"Why now: {t.why}", markup=False, highlight=False)
        if t.my_connection:
            console.print(f"My experience: {t.my_connection}", markup=False, highlight=False)
        if t.trend_terms:
            console.print("Trends: " + ", ".join(t.trend_terms), style="dim", markup=False)


@app.command()
def suggest(
    days: Optional[int] = typer.Option(None, "--days", "-d", help="Use material from the last N days."),
    count: Optional[int] = typer.Option(None, "--count", "-n", help="Number of suggestions (default: pipeline.suggest_count)."),
    collect_first: bool = typer.Option(False, "--collect", help="Run all collectors first."),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Step 1: suggest topics from tech trends + your own AI usage. Pick them with `xdraft write`."""
    from xdraft.pipeline.generate import suggest_topics, trend_snapshot
    from xdraft.pipeline.trends import format_trends

    cfg = load_config(config)
    since = utcnow() - timedelta(days=days if days is not None else cfg.lookback_days)
    if collect_first:
        run_collect(cfg, since)
    engine = get_engine(cfg.resolve_path(cfg.db_path))
    with Session(engine) as session:
        snap = trend_snapshot(cfg, session, since)
        console.rule("Trend signals")
        console.print(format_trends(snap.terms), markup=False, highlight=False)
        console.rule("Your AI usage")
        console.print(snap.usage, markup=False, highlight=False)
        try:
            topics = suggest_topics(cfg, make_llm(cfg.llm), session, since, count)
        except LLMError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e
        print_topics(topics)
    if not topics:
        console.print("No suggestions (no material in the window). Try `xdraft collect` first.")
        return
    console.print(f"\nPick topics in the dashboard (`xdraft serve`) or run: xdraft write {topics[0].id} …")


@app.command()
def topics(
    status: str = typer.Option("suggested", "--status", help="suggested | drafted | dismissed | all"),
    limit: int = typer.Option(20, "--limit", "-n"),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """List topic suggestions."""
    cfg = load_config(config)
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as session:
        q = select(Topic).order_by(Topic.id.desc()).limit(limit)
        if status != "all":
            q = q.where(Topic.status == status)
        print_topics(list(session.exec(q).all()))


@app.command()
def write(
    topic_ids: list[int] = typer.Argument(..., help="Topic ids to write (see `xdraft topics`)."),
    take: str = typer.Option("", "--take", "-t", help="Your own opinion; the post will argue it."),
    fmt: str = typer.Option("", "--format", "-f", help="thread (default) or quick (one tweet)."),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Steps 2-4: write → polish → fix bilingual drafts for the picked topics."""
    from xdraft.pipeline.generate import produce_drafts

    cfg = load_config(config)
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as session:
        llm = make_llm(cfg.llm)
        topic_map = {t.id: t for t in session.exec(select(Topic)).all()}
        for tid in topic_ids:
            topic = topic_map.get(tid)
            if not topic:
                console.print(f"[yellow]No topic #{tid}[/yellow]")
                continue
            if take.strip():
                topic.user_take = take.strip()
            if fmt in ("thread", "quick"):
                topic.format = fmt
            try:
                created = produce_drafts(cfg, llm, session, topic)
            except LLMError as e:
                console.print(f"[red]topic #{tid}: {e}[/red]")
                continue
            print_drafts(created, topic_map)
    console.print("\nReview and approve them in the dashboard: `xdraft serve`.")


@app.command()
def run(config: Optional[Path] = ConfigOpt) -> None:
    """Automation entry point (cron): collect → trends → topic suggestions, then publish due posts."""
    from xdraft.pipeline.generate import suggest_topics
    from xdraft.publish.service import publish_due

    cfg = load_config(config)
    since = utcnow() - timedelta(days=cfg.lookback_days)
    run_collect(cfg, since)
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as session:
        try:
            topics = suggest_topics(cfg, make_llm(cfg.llm), session, since)
            console.print(f"{len(topics)} new topic suggestions waiting in the dashboard.")
        except LLMError as e:
            console.print(f"[red]{e}[/red]")
        for d in publish_due(cfg, session):
            console.print(f"draft #{d.id} [{d.lang}] -> {d.status}")


@app.command()
def drafts(
    status: Optional[str] = typer.Option(DraftStatus.PENDING, "--status", help=f"One of {DraftStatus.ALL} or 'all'."),
    limit: int = typer.Option(20, "--limit", "-n"),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """List drafts."""
    cfg = load_config(config)
    engine = get_engine(cfg.resolve_path(cfg.db_path))
    with Session(engine) as session:
        q = select(Draft).order_by(Draft.id.desc()).limit(limit)
        if status and status != "all":
            q = q.where(Draft.status == status)
        rows = list(session.exec(q).all())
        topic_map = {t.id: t for t in session.exec(select(Topic)).all()}
    print_drafts(rows, topic_map)


@app.command()
def serve(
    port: int = typer.Option(8000, "--port", "-p"),
    no_scheduler: bool = typer.Option(False, "--no-scheduler", help="Don't publish scheduled posts from this process."),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Start the local review dashboard on http://127.0.0.1:PORT."""
    import uvicorn

    from xdraft.web.app import create_app

    cfg = load_config(config)
    mode = "LIVE posting" if cfg.publish.enabled else "dry run (publish.enabled is false)"
    console.print(f"Dashboard: http://127.0.0.1:{port}  —  {mode}")
    uvicorn.run(create_app(cfg, start_scheduler=not no_scheduler), host="127.0.0.1", port=port, log_level="warning")


@app.command("publish-due")
def publish_due_cmd(config: Optional[Path] = ConfigOpt) -> None:
    """Publish approved drafts whose time has come (for cron, if the dashboard isn't running)."""
    from xdraft.publish.service import publish_due

    cfg = load_config(config)
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as session:
        done = publish_due(cfg, session)
    for d in done:
        console.print(f"draft #{d.id} [{d.lang}] -> {d.status}" + (f" ({d.publish_error})" if d.publish_error else ""))
    if not done:
        console.print("Nothing due.")


@app.command()
def metrics(config: Optional[Path] = ConfigOpt) -> None:
    """Fetch likes/reposts/replies/views for recent published posts."""
    from xdraft.publish.service import refresh_metrics
    from xdraft.publish.x import XError

    cfg = load_config(config)
    try:
        with Session(get_engine(cfg.resolve_path(cfg.db_path))) as session:
            n = refresh_metrics(cfg, session)
    except XError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    console.print(f"Updated metrics for {n} posts.")


@app.command("x-status")
def x_status(config: Optional[Path] = ConfigOpt) -> None:
    """Show whether X posting is configured and connected."""
    from xdraft.publish.service import make_x_client
    from xdraft.publish.x import XError

    cfg = load_config(config)
    client = make_x_client(cfg)
    console.print(f"publish.enabled: {cfg.publish.enabled}")
    console.print(f"{cfg.publish.client_id_env} set: {bool(client.client_id)}")
    if not client.connected:
        console.print("Not connected — run `xdraft serve` and click “Connect X”.")
        return
    try:
        me = client.me()
        console.print(f"Connected as @{me.get('username', '?')}")
    except XError as e:
        console.print(f"[red]{e}[/red]")


@app.command("style-import")
def style_import(
    archive: Path = typer.Argument(..., help="X archive folder (or its data/tweets.js)."),
    samples: int = typer.Option(80, "--samples", help="Posts sent to the LLM."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Only copy example posts; don't call the LLM."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing style file."),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Learn your voice from your X archive: saves your posts as examples and writes a style guide (style.md)."""
    from xdraft import voice
    from xdraft.style_import import build_style, examples_only, load_archive_tweets

    cfg = load_config(config)
    tweets = load_archive_tweets(archive)
    if not tweets:
        console.print("No original posts found in the archive.")
        raise typer.Exit(1)
    saved = voice.save_archive(cfg, tweets)
    console.print(f"Saved {len(tweets)} of your posts as writing examples: {saved}", markup=False)

    out = cfg.resolve_path(cfg.draft.style_path)
    template = cfg.resolve_path("style.example.md")
    is_template = out.exists() and template.exists() and out.read_text(encoding="utf-8") == template.read_text(encoding="utf-8")
    if out.exists() and not (force or is_template):
        console.print(f"Kept your existing {out.name} (use --force to regenerate it).", markup=False)
        return
    if no_llm:
        text = examples_only(tweets)
    else:
        try:
            text = build_style(make_llm(cfg.llm), tweets, samples)
        except LLMError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    console.print(f"Wrote {out} from {len(tweets)} posts. Read it and fix anything that isn't you.", markup=False)


voice_app = typer.Typer(help="Writing examples that make drafts sound like you.", no_args_is_help=True)
app.add_typer(voice_app, name="voice")


@voice_app.command("show")
def voice_show(config: Optional[Path] = ConfigOpt) -> None:
    """How many writing examples xdraft has."""
    from xdraft import voice

    info = voice.summary(load_config(config))
    console.print(f"Your writing: {info['own']} pieces ({info['own_zh']} Chinese, {info['own_en']} English)", markup=False)
    console.print(f"Writers you like: {info['references']} excerpts", markup=False)
    console.print(f"Folder: {info['dir']}", markup=False)
    if info["own"] < 10:
        console.print("Tip: add at least 10-20 pieces of your own writing; it is the biggest lever against AI-sounding drafts.")


@voice_app.command("add-file")
def voice_add_file(
    path: Path = typer.Argument(..., help="A .md or .txt file. Separate several pieces with a line of ---."),
    reference: bool = typer.Option(False, "--reference", help="It's by a writer you like (default: your own writing)."),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Add a text file of your own writing (or, with --reference, someone you like)."""
    from xdraft import voice

    cfg = load_config(config)
    text = path.read_text(encoding="utf-8")
    out = voice.save_text(cfg, "reference" if reference else "mine", text, path.stem)
    console.print(f"Saved {out}", markup=False)


@voice_app.command("add-url")
def voice_add_url(url: str = typer.Argument(...), config: Optional[Path] = ConfigOpt) -> None:
    """Save the text of an article by a writer you like (blogs, Substack, Medium…; not x.com, which needs login)."""
    from xdraft import voice

    try:
        out = voice.save_url(load_config(config), url)
    except Exception as e:  # noqa: BLE001 - network / extraction errors are shown to the user
        console.print(f"[red]Could not save {url}: {e}[/red]")
        raise typer.Exit(1) from e
    console.print(f"Saved {out}", markup=False)


@app.command()
def init(force: bool = typer.Option(False, "--force", help="Overwrite existing config.yaml / .env / style.md.")) -> None:
    """First-time setup: create config.yaml, .env and style.md from the examples (run in the repo folder)."""
    from xdraft.setup_cmds import init_project

    repo = Path.cwd()
    if not (repo / "config.example.yaml").is_file():
        console.print("Run this inside the X-auto-draft-post folder (config.example.yaml not found here).")
        raise typer.Exit(1)
    for path, what in init_project(repo, force):
        console.print(f"{path:<14} {what}", markup=False)
    console.print(
        "\nNext:\n"
        "  1. Put your DeepSeek (or OpenAI) key in .env        notepad .env\n"
        "  2. Set git.repos and anything else in config.yaml   notepad config.yaml\n"
        "  3. Start the dashboard                              uv run xdraft serve",
        markup=False,
    )


@app.command()
def schedule(
    action: str = typer.Argument("show", help="install | remove | show"),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Run the pipeline without keeping the dashboard open: Windows Task Scheduler (or cron elsewhere)."""
    import subprocess

    from xdraft import setup_cmds

    if action not in ("install", "remove", "show"):
        raise typer.BadParameter("action must be install, remove or show")
    cfg = load_config(config)
    cfg_path = (config or Path("config.yaml")).resolve()
    if not cfg_path.is_file():
        console.print("config.yaml not found: run `uv run xdraft init` first (in the repo folder).")
        raise typer.Exit(1)
    log_path = cfg.resolve_path("data/scheduled.log")
    jobs = setup_cmds.jobs(cfg)

    if not setup_cmds.is_windows():
        python = sys.executable
        console.print("Not on Windows. Add these lines with `crontab -e` (times are this machine's local time):\n")
        for line in setup_cmds.cron_lines(cfg, cfg_path, log_path, python):
            console.print(line, markup=False, highlight=False, soft_wrap=True)
        return

    python = setup_cmds.windowless_python()
    for job in jobs:
        command = setup_cmds.task_command(cfg_path, log_path, job, python, windows=True)
        if action == "install":
            command = setup_cmds.fit_task_command(command, job, log_path.parent)
        if action == "show":
            console.print(f"{job.name}: {job.description}\n  {' '.join(job.windows_schedule)}\n  {command}\n",
                          markup=False, highlight=False, soft_wrap=True)
            continue
        args = (setup_cmds.schtasks_create_args(job, command) if action == "install"
                else setup_cmds.schtasks_delete_args(job))
        result = subprocess.run(args, capture_output=True, text=True)
        status = "ok" if result.returncode == 0 else f"failed: {(result.stderr or result.stdout).strip()}"
        console.print(f"{action} {job.name}: {status}", markup=False)
    if action == "install":
        console.print(f"\nTimes are this PC's local time. Output goes to {log_path}. "
                      "Remove with: uv run xdraft schedule remove", markup=False)


if __name__ == "__main__":
    app()
