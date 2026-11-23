"""SQLite engine and item persistence."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from xdraft.collectors.base import CollectedItem
from xdraft.models import Item
from xdraft.timeutil import to_utc, utcnow


def get_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    return engine


@dataclass
class UpsertCounts:
    new: int = 0
    updated: int = 0
    unchanged: int = 0


def upsert_items(session: Session, items: Iterable[CollectedItem]) -> UpsertCounts:
    """Insert new items, update ones whose content changed (e.g. a session that grew)."""
    counts = UpsertCounts()
    now = to_utc(utcnow())
    for ci in items:
        h = ci.content_hash
        existing = session.exec(
            select(Item).where(Item.source == ci.source, Item.source_id == ci.source_id)
        ).first()
        if existing is None:
            session.add(Item(
                source=ci.source, source_id=ci.source_id, ts=to_utc(ci.ts),
                title=ci.title, body=ci.body, url=ci.url, meta=ci.meta,
                content_hash=h, collected_at=now, updated_at=now,
            ))
            counts.new += 1
        elif existing.content_hash != h:
            existing.ts = to_utc(ci.ts)
            existing.title, existing.body, existing.url = ci.title, ci.body, ci.url
            existing.meta = ci.meta
            existing.content_hash = h
            existing.updated_at = now
            session.add(existing)
            counts.updated += 1
        else:
            counts.unchanged += 1
    session.commit()
    return counts
