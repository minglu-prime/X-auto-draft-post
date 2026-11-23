"""Run collectors and persist their output."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import Session

from xdraft.collectors import COLLECTORS
from xdraft.config import Config
from xdraft.db import UpsertCounts, get_engine, upsert_items

log = logging.getLogger(__name__)


@dataclass
class SourceReport:
    source: str
    skipped: bool = False
    collected: int = 0
    counts: UpsertCounts = field(default_factory=UpsertCounts)
    errors: list[str] = field(default_factory=list)


def enabled_sources(cfg: Config) -> list[str]:
    flags = {
        "claude_code": cfg.claude_code.enabled,
        "notes": cfg.notes.enabled,
        "git": cfg.git.enabled,
        "articles": cfg.articles.enabled,
    }
    return [name for name in COLLECTORS if flags.get(name, True)]


def run_collect(cfg: Config, since: datetime, sources: list[str] | None = None) -> list[SourceReport]:
    engine = get_engine(cfg.resolve_path(cfg.db_path))
    wanted = sources or enabled_sources(cfg)
    reports = []
    for name in COLLECTORS:
        report = SourceReport(source=name)
        reports.append(report)
        if name not in wanted:
            report.skipped = True
            continue
        try:
            result = COLLECTORS[name](cfg, since)
        except Exception as e:  # noqa: BLE001 - one broken source must not stop the others
            log.exception("collector %s crashed", name)
            report.errors.append(f"crashed: {e}")
            continue
        report.collected = len(result.items)
        report.errors = result.errors
        with Session(engine) as session:
            report.counts = upsert_items(session, result.items)
    return reports
