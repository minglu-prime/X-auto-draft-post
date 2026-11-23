import os
import time

from xdraft.collectors import notes


def test_notes_with_and_without_frontmatter(cfg, since):
    inbox = cfg.resolve_path("inbox")
    inbox.mkdir()
    (inbox / "take.md").write_text(
        "---\ntags: [claude, coding]\nurl: https://example.com/a\n---\n"
        "# New model is great at refactors\nSee https://example.com/b. Contact me@example.com\n",
        encoding="utf-8",
    )
    (inbox / "plain.txt").write_text("quick thought about agents", encoding="utf-8")
    (inbox / "_template.md").write_text("ignored", encoding="utf-8")
    old = inbox / "old.md"
    old.write_text("old note", encoding="utf-8")
    t = time.time() - 30 * 86400
    os.utime(old, (t, t))

    items = {i.source_id: i for i in notes.collect(cfg, since).items}
    assert set(items) == {"take.md", "plain.txt"}
    take = items["take.md"]
    assert take.title == "New model is great at refactors"
    assert take.meta["tags"] == ["claude", "coding"]
    assert take.meta["urls"] == ["https://example.com/a", "https://example.com/b"]
    assert take.url == "https://example.com/a"
    assert "me@example.com" not in take.body
    assert items["plain.txt"].title == "plain"


def test_frontmatter_date_controls_window(cfg, since):
    inbox = cfg.resolve_path("inbox")
    inbox.mkdir()
    (inbox / "dated.md").write_text("---\ndate: 2020-01-01\n---\nold idea", encoding="utf-8")
    assert notes.collect(cfg, since).items == []
