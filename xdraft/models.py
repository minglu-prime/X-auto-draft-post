"""Database models."""

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


class Topic(SQLModel, table=True):
    """A post idea chosen by the LLM from a set of items."""

    id: int | None = Field(default=None, primary_key=True)
    title: str
    angle: str  # the personal take / hook
    why: str = ""
    score: int = 0
    item_ids: list = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime


class DraftStatus:
    PENDING = "pending"        # waiting for review
    SCHEDULED = "scheduled"    # approved, publishes at scheduled_at
    PUBLISHED = "published"    # posted to X
    DRY_RUN = "dry_run"        # "published" with publishing disabled
    FAILED = "failed"
    REJECTED = "rejected"

    ALL = (PENDING, SCHEDULED, PUBLISHED, DRY_RUN, FAILED, REJECTED)


class Draft(SQLModel, table=True):
    """One language version of a post (a single tweet or a thread)."""

    id: int | None = Field(default=None, primary_key=True)
    topic_id: int | None = Field(default=None, foreign_key="topic.id", index=True)
    lang: str  # zh | en
    tweets: list = Field(default_factory=list, sa_column=Column(JSON))
    source_urls: list = Field(default_factory=list, sa_column=Column(JSON))
    warnings: list = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default=DraftStatus.PENDING, index=True)
    edited: bool = False
    scheduled_at: datetime | None = None
    published_at: datetime | None = None
    tweet_ids: list = Field(default_factory=list, sa_column=Column(JSON))
    publish_error: str | None = None
    metrics: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime
    updated_at: datetime

    @property
    def text(self) -> str:
        return "\n\n".join(self.tweets)
