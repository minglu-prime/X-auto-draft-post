"""Database models. Topic / Draft / ScheduledPost arrive in later milestones."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


class Item(SQLModel, table=True):
    """One piece of raw material from a collector (a session, a note, a day of commits, an article)."""

    __table_args__ = (UniqueConstraint("source", "source_id"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)  # claude_code | notes | git | articles
    source_id: str
    ts: datetime = Field(index=True)  # UTC
    title: str
    body: str
    url: str | None = None
    meta: dict = Field(default_factory=dict, sa_column=Column(JSON))
    content_hash: str
    collected_at: datetime
    updated_at: datetime
