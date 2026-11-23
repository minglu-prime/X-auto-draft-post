"""Command line: `xdraft collect`, `xdraft items`."""

from __future__ import annotations

import logging
from datetime import timedelta
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
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


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


@app.command()
def draft(
    days: Optional[int] = typer.Option(None, "--days", "-d", help="Use material from the last N days."),
    topics: Optional[int] = typer.Option(None, "--topics", "-t", help="Number of topics (default: draft.topics_per_run)."),
    collect_first: bool = typer.Option(False, "--collect", help="Run all collectors first."),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Pick topics from collected material and write bilingual drafts for review."""
    from xdraft.pipeline.generate import generate

    cfg = load_config(config)
    since = utcnow() - timedelta(days=days if days is not None else cfg.lookback_days)
    if collect_first:
        run_collect(cfg, since)
    engine = get_engine(cfg.resolve_path(cfg.db_path))
    try:
        llm = make_llm(cfg.llm)
        with Session(engine) as session:
            created = generate(cfg, llm, session, since, topics)
            topic_map = {t.id: t for t in session.exec(select(Topic)).all()}
    except LLMError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    if not created:
        console.print("No drafts created (no material in the window, or no topic found). Try `xdraft collect` first.")
        return
    print_drafts(created, topic_map)
    console.print(f"\n{len(created)} drafts are waiting for review: run `xdraft serve` and open http://127.0.0.1:8000")


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
    """Learn your writing style from your X archive and write it to style.md."""
    from xdraft.style_import import build_style, examples_only, load_archive_tweets

    cfg = load_config(config)
    out = cfg.resolve_path(cfg.draft.style_path)
    if out.exists() and not force:
        console.print(f"{out} already exists; use --force to overwrite.")
        raise typer.Exit(1)
    tweets = load_archive_tweets(archive)
    if not tweets:
        console.print("No original posts found in the archive.")
        raise typer.Exit(1)
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
    console.print(f"Wrote {out} from {len(tweets)} posts. Review and edit it — drafts follow it closely.")


if __name__ == "__main__":
    app()
