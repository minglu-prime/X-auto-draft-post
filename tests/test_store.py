from datetime import timedelta

from conftest import NOW, write_session
from sqlmodel import Session, select

from xdraft.models import Item
from xdraft.runner import run_collect


def test_run_collect_upserts(cfg, since):
    root = cfg.resolve_path("claude_projects")
    path = write_session(root, "p", "sess-1", "/w/proj", NOW - timedelta(hours=1))
    inbox = cfg.resolve_path("inbox")
    inbox.mkdir()
    (inbox / "n.md").write_text("an idea", encoding="utf-8")

    reports = {r.source: r for r in run_collect(cfg, since, ["claude_code", "notes"])}
    assert reports["claude_code"].counts.new == 1 and reports["notes"].counts.new == 1
    assert reports["git"].skipped and reports["articles"].skipped

    again = {r.source: r for r in run_collect(cfg, since, ["claude_code", "notes"])}
    assert again["claude_code"].counts.unchanged == 1

    # The session keeps growing -> the same item is updated, not duplicated.
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"user","sessionId":"sess-1","cwd":"/w/proj","timestamp":"%s",'
                 '"message":{"role":"user","content":"one more thing"}}\n'
                 % NOW.isoformat().replace("+00:00", "Z"))
    third = {r.source: r for r in run_collect(cfg, since, ["claude_code"])}
    assert third["claude_code"].counts.updated == 1

    from xdraft.db import get_engine
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        rows = s.exec(select(Item)).all()
    assert len(rows) == 2
    cc = next(r for r in rows if r.source == "claude_code")
    assert "one more thing" in cc.body and cc.meta["n_user_prompts"] == 3
