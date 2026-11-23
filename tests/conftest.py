import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from xdraft.config import Config

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture
def since() -> datetime:
    return NOW - timedelta(days=3)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config.model_validate({
        "db_path": "data/test.db",
        "claude_code": {"projects_dir": "claude_projects"},
        "notes": {"inbox_dir": "inbox"},
        "articles": {"feeds": [], "hackernews": {"enabled": False}, "github": {"enabled": False},
                     "fetch_fulltext": False},
        "voice": {"dir": "voice"},
    })
    c._base_dir = tmp_path
    return c


def write_session(projects_dir: Path, folder: str, session_id: str, cwd: str, start: datetime, records=None) -> Path:
    d = projects_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    t = lambda m: iso(start + timedelta(minutes=m))  # noqa: E731
    base = {"sessionId": session_id, "cwd": cwd, "gitBranch": "main"}
    recs = records if records is not None else [
        {**base, "type": "user", "timestamp": t(0),
         "message": {"role": "user", "content": "Refactor the parser to stream input. My key is sk-ant-api03-SECRETSECRETSECRET123"}},
        {**base, "type": "assistant", "timestamp": t(1),
         "message": {"role": "assistant", "model": "claude-opus-5-5",
                     "content": [{"type": "text", "text": "Sure"},
                                 {"type": "tool_use", "name": "Read", "input": {}},
                                 {"type": "tool_use", "name": "Edit", "input": {}}],
                     "usage": {"input_tokens": 100, "output_tokens": 50}}},
        {**base, "type": "user", "timestamp": t(2),
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "file contents"}]}},
        {**base, "type": "user", "timestamp": t(3), "isMeta": True,
         "message": {"role": "user", "content": "meta stuff"}},
        {**base, "type": "user", "timestamp": t(4),
         "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
        {**base, "type": "user", "timestamp": t(5), "isSidechain": True,
         "message": {"role": "user", "content": "subagent prompt"}},
        {**base, "type": "user", "timestamp": t(6),
         "message": {"role": "user", "content": [{"type": "text", "text": "Now add tests in /home/alice/proj/tests"}]}},
        {**base, "type": "assistant", "timestamp": t(30),
         "message": {"role": "assistant", "model": "claude-sonnet-5-5",
                     "content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
    ]
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\nnot json\n", encoding="utf-8")
    return p
