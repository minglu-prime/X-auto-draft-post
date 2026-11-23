"""Shared types for collectors."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CollectedItem:
    source: str
    source_id: str
    ts: datetime  # aware UTC
    title: str
    body: str
    url: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            [self.title, self.body, self.url, self.meta],
            sort_keys=True, ensure_ascii=False, default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CollectResult:
    items: list[CollectedItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
