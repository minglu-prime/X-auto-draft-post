"""Read quick notes from notes/inbox/*.md (optional YAML frontmatter).

Frontmatter keys (all optional): title, date, tags, url / urls.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from xdraft.collectors.base import CollectedItem, CollectResult
from xdraft.config import Config
from xdraft.redact import redact
from xdraft.timeutil import parse_iso

SOURCE = "notes"
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def parse_note(text: str) -> tuple[dict, str]:
    """Split optional `---` YAML frontmatter from the body."""
    if text.startswith("---"):
        m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.S)
        if m:
            try:
                fm = yaml.safe_load(m.group(1)) or {}
            except yaml.YAMLError:
                fm = {}
            if isinstance(fm, dict):
                return fm, text[m.end():]
    return {}, text


def _title(fm: dict, body: str, path: Path) -> str:
    if fm.get("title"):
        return str(fm["title"])
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def collect(cfg: Config, since: datetime) -> CollectResult:
    result = CollectResult()
    inbox = cfg.resolve_path(cfg.notes.inbox_dir)
    if not inbox.is_dir():
        result.errors.append(f"Notes inbox not found: {inbox}")
        return result
    extra = cfg.redaction.extra_patterns
    for path in sorted(inbox.rglob("*")):
        if path.suffix.lower() not in {".md", ".txt"} or path.name.startswith((".", "_")):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            result.errors.append(f"{path.name}: {e}")
            continue
        fm, body = parse_note(text)
        ts = parse_iso(fm.get("date") or fm.get("created")) or datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        )
        if ts < since or not body.strip():
            continue
        urls = _as_list(fm.get("urls")) + _as_list(fm.get("url"))
        urls += [u.rstrip(".,;:!?") for u in _URL_RE.findall(body)]
        urls = list(dict.fromkeys(urls))
        result.items.append(CollectedItem(
            source=SOURCE,
            source_id=path.relative_to(inbox).as_posix(),
            ts=ts,
            title=redact(_title(fm, body, path), extra),
            body=redact(body.strip(), extra),
            url=urls[0] if urls else None,
            meta={"tags": _as_list(fm.get("tags")), "urls": urls},
        ))
    return result
