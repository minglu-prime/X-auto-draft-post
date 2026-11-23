"""Summarize recent Claude Code sessions from ~/.claude/projects/*/*.jsonl.

One Item per session: project, models used, tool usage and (redacted) user prompts.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from xdraft.collectors.base import CollectedItem, CollectResult
from xdraft.config import Config
from xdraft.redact import redact
from xdraft.timeutil import parse_iso

log = logging.getLogger(__name__)

SOURCE = "claude_code"


@dataclass
class SessionSummary:
    session_id: str
    project: str
    branch: str | None = None
    title: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    models: Counter = field(default_factory=Counter)
    tools: Counter = field(default_factory=Counter)
    prompts: list[str] = field(default_factory=list)
    n_user_prompts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def _prompt_text(content: object) -> str | None:
    """Extract what the human typed; ignore tool results, commands and system wrappers."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return None
                continue
            parts.append(block.get("text", ""))
        text = "\n".join(parts)
    else:
        return None
    text = text.strip()
    if not text or text.startswith("<") or text.startswith("Caveat:"):
        return None
    return text


def summarize_session(path: Path) -> SessionSummary | None:
    s = SessionSummary(session_id=path.stem, project=path.parent.name)
    seen_cwd = False
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")
            if rtype == "summary" and rec.get("summary"):
                s.title = s.title or str(rec["summary"])
                continue
            ts = parse_iso(rec.get("timestamp"))
            if ts:
                s.start = min(s.start, ts) if s.start else ts
                s.end = max(s.end, ts) if s.end else ts
            if rec.get("sessionId"):
                s.session_id = str(rec["sessionId"])
            if rec.get("cwd") and not seen_cwd:
                s.project = Path(str(rec["cwd"])).name or s.project
                seen_cwd = True
            if rec.get("gitBranch"):
                s.branch = str(rec["gitBranch"])
            if rec.get("isSidechain"):
                continue  # subagent traffic, not the human's own conversation
            msg = rec.get("message") or {}
            if not isinstance(msg, dict):
                continue
            if rtype == "user" and not rec.get("isMeta"):
                text = _prompt_text(msg.get("content"))
                if text:
                    s.n_user_prompts += 1
                    s.prompts.append(text)
            elif rtype == "assistant":
                model = msg.get("model")
                if model and not str(model).startswith("<"):
                    s.models[str(model)] += 1
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        s.tools[str(block.get("name", "?"))] += 1
                usage = msg.get("usage") or {}
                if isinstance(usage, dict):
                    s.input_tokens += int(usage.get("input_tokens") or 0)
                    s.output_tokens += int(usage.get("output_tokens") or 0)
    if s.end is None:
        return None
    return s


def _project_allowed(project: str, include: list[str], exclude: list[str]) -> bool:
    if include and not any(fnmatch.fnmatch(project, pat) for pat in include):
        return False
    return not any(fnmatch.fnmatch(project, pat) for pat in exclude)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _to_item(s: SessionSummary, cfg: Config) -> CollectedItem:
    cc = cfg.claude_code
    extra = cfg.redaction.extra_patterns
    prompts = [redact(_truncate(p, cc.max_prompt_chars), extra) for p in s.prompts]
    shown = prompts[: cc.max_prompts_per_session]
    duration_min = round((s.end - s.start).total_seconds() / 60) if s.start and s.end else 0
    models = ", ".join(f"{m} ×{n}" for m, n in s.models.most_common()) or "unknown"
    tools = ", ".join(f"{t} ×{n}" for t, n in s.tools.most_common(10)) or "none"

    lines = [
        f"Project: {s.project}" + (f" (branch {s.branch})" if s.branch else ""),
        f"Time: {s.start:%Y-%m-%d %H:%M} → {s.end:%H:%M} UTC ({duration_min} min)",
        f"Models: {models}",
        f"Tools: {tools}",
        f"User prompts ({s.n_user_prompts}):",
        *(f"{i}. {p}" for i, p in enumerate(shown, 1)),
    ]
    if len(prompts) > len(shown):
        lines.append(f"… {len(prompts) - len(shown)} more")

    title = s.title or (shown[0] if shown else f"Session in {s.project}")
    return CollectedItem(
        source=SOURCE,
        source_id=s.session_id,
        ts=s.end,
        title=redact(_truncate(title, 100), extra),
        body="\n".join(lines),
        meta={
            "project": s.project,
            "branch": s.branch,
            "models": dict(s.models),
            "tools": dict(s.tools),
            "n_user_prompts": s.n_user_prompts,
            "duration_min": duration_min,
            "input_tokens": s.input_tokens,
            "output_tokens": s.output_tokens,
            "start": s.start.isoformat() if s.start else None,
        },
    )


def collect(cfg: Config, since: datetime) -> CollectResult:
    cc = cfg.claude_code
    result = CollectResult()
    root = cfg.resolve_path(cc.projects_dir)
    if not root.is_dir():
        result.errors.append(f"Claude Code projects dir not found: {root}")
        return result
    for path in sorted(root.glob("*/*.jsonl")):
        try:
            if path.stat().st_mtime < since.timestamp():
                continue
            s = summarize_session(path)
        except OSError as e:
            result.errors.append(f"{path.name}: {e}")
            continue
        if s is None or s.end < since:
            continue
        if s.n_user_prompts < cc.min_user_prompts:
            continue
        if not _project_allowed(s.project, cc.include_projects, cc.exclude_projects):
            continue
        result.items.append(_to_item(s, cfg))
    return result
