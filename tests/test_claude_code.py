from datetime import timedelta

from conftest import NOW, write_session

from xdraft.collectors import claude_code


def test_session_summary(cfg, since):
    root = cfg.resolve_path("claude_projects")
    write_session(root, "-home-alice-myproj", "sess-1", "/home/alice/myproj", NOW - timedelta(hours=2))
    res = claude_code.collect(cfg, since)
    assert res.errors == []
    assert len(res.items) == 1
    item = res.items[0]
    assert item.source_id == "sess-1"
    assert item.meta["project"] == "myproj"
    assert item.meta["models"] == {"claude-opus-5-5": 1, "claude-sonnet-5-5": 1}
    assert item.meta["tools"] == {"Read": 1, "Edit": 1, "Bash": 1}
    assert item.meta["n_user_prompts"] == 2  # tool_result, meta, command, sidechain excluded
    assert item.meta["duration_min"] == 30
    assert "sk-ant" not in item.body and "[REDACTED_KEY]" in item.body
    assert "alice" not in item.body
    assert "subagent prompt" not in item.body
    assert item.title.startswith("Refactor the parser")
    assert "Last assistant reply (what got done): Sure" in item.body


def test_filters_old_and_excluded_projects(cfg, since):
    root = cfg.resolve_path("claude_projects")
    write_session(root, "old", "sess-old", "/w/oldproj", NOW - timedelta(days=10))
    write_session(root, "secret", "sess-secret", "/w/client-secret", NOW - timedelta(hours=1))
    write_session(root, "ok", "sess-ok", "/w/okproj", NOW - timedelta(hours=1))
    cfg.claude_code.exclude_projects = ["client-*"]
    ids = {i.source_id for i in claude_code.collect(cfg, since).items}
    assert ids == {"sess-ok"}


def test_missing_dir_reports_error(cfg, since):
    res = claude_code.collect(cfg, since)
    assert res.items == [] and "not found" in res.errors[0]
