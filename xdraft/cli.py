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
from xdraft.models import Item
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


if __name__ == "__main__":
    app()
