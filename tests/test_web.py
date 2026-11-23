from datetime import timedelta

import pytest
from conftest import NOW
from fakes import FakeLLM
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from xdraft.collectors.base import CollectedItem
from xdraft.db import get_engine, upsert_items
from xdraft.models import Draft, DraftStatus, Item, Topic, TopicStatus
from xdraft.pipeline.schemas import BilingualDraft, OptimizedDraft, OptimizedVersion, PostVersion, SuggestionPlan, TopicSuggestion
from xdraft.timeutil import utcnow
from xdraft.web.app import create_app

H = {"X-Xdraft": "1"}


@pytest.fixture
def setup(cfg):
    llm = FakeLLM()
    app = create_app(cfg, llm_factory=lambda: llm, start_scheduler=False,
                     allowed_hosts=("testserver", "127.0.0.1"))
    engine = get_engine(cfg.resolve_path(cfg.db_path))
    with Session(engine) as s:
        upsert_items(s, [CollectedItem("notes", "n1", NOW - timedelta(hours=1), "Model take",
                                       "The new model kept the plan across 40 files", url="https://n.example")])
        item_id = s.exec(select(Item)).one().id
        topic = Topic(title="Refactors", angle="plans hold", why="", score=70, item_ids=[item_id], created_at=utcnow())
        s.add(topic)
        s.commit()
        for lang, text in (("zh", "四十个文件的重构，计划没跑偏。"), ("en", "40-file refactor, plan held.")):
            s.add(Draft(topic_id=topic.id, lang=lang, tweets=[text], created_at=utcnow(), updated_at=utcnow()))
        s.commit()
        ids = [d.id for d in s.exec(select(Draft)).all()]
    return TestClient(app), llm, engine, ids


def _get(engine, draft_id):
    with Session(engine) as s:
        return s.get(Draft, draft_id)


def test_guard_blocks_foreign_host_and_missing_header(setup):
    client, _llm, _engine, ids = setup
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
    assert client.post(f"/drafts/{ids[0]}/reject").status_code == 403  # no X-Xdraft header (CSRF)
    assert client.get("/").status_code == 200


def test_stepper_starts_at_collect_on_empty_db(cfg):
    client = TestClient(create_app(cfg, llm_factory=FakeLLM, start_scheduler=False, allowed_hosts=("testserver",)))
    html = client.get("/").text
    assert 'data-current="collect"' in html and "收集素材" in html


def test_stepper_points_to_review_when_drafts_wait(setup):
    client, *_ = setup
    html = client.get("/").text
    assert 'data-current="review"' in html and "待审稿" in html


def test_index_shows_drafts(setup):
    client, *_ = setup
    html = client.get("/").text
    assert "Refactors" in html and "四十个文件" in html and "演练模式" in html
    for page in ("/sources", "/inbox", "/history"):
        assert client.get(page).status_code == 200


def test_save_splits_thread_and_rechecks(setup):
    client, _llm, engine, ids = setup
    r = client.post(f"/drafts/{ids[1]}/save", data={"body": "first 99% claim\n---\nsecond"}, headers=H)
    assert r.status_code == 200 and "已保存" in r.text
    d = _get(engine, ids[1])
    assert d.tweets == ["first 99% claim", "second"] and d.edited
    assert any("99%" in w for w in d.warnings)


def test_approve_now_is_dry_run(setup):
    client, _llm, engine, ids = setup
    r = client.post(f"/drafts/{ids[0]}/approve", data={"mode": "now"}, headers=H)
    assert "演练模式" in r.text
    assert _get(engine, ids[0]).status == DraftStatus.DRY_RUN


def test_schedule_converts_local_time(setup):
    client, _llm, engine, ids = setup
    # UTC+8 browser: getTimezoneOffset() = -480
    r = client.post(f"/drafts/{ids[0]}/approve",
                    data={"mode": "custom", "when": "2030-01-02T09:30", "tz_offset": "-480"}, headers=H)
    assert "已排期：周三 01-02 09:30（Asia/Shanghai）" in r.text
    assert _get(engine, ids[0]).scheduled_at.hour == 1  # stored as 01:30 UTC
    client.post(f"/drafts/{ids[0]}/unschedule", headers=H)
    assert _get(engine, ids[0]).status == DraftStatus.PENDING


def test_reject(setup):
    client, _llm, engine, ids = setup
    client.post(f"/drafts/{ids[0]}/reject", headers=H)
    assert _get(engine, ids[0]).status == DraftStatus.REJECTED


def test_rewrite_uses_llm(setup):
    client, llm, engine, ids = setup
    llm.replies.append(PostVersion(tweets=["shorter"], source_urls=[]))
    r = client.post(f"/drafts/{ids[1]}/rewrite", data={"preset": "shorter"}, headers=H)
    assert "已改写" in r.text and _get(engine, ids[1]).tweets == ["shorter"]
    system, user, _ = llm.calls[-1]
    assert "English" in system and "Make it shorter" in user and "40 files" in user


def test_inbox_writes_note(setup, cfg):
    client, *_ = setup
    r = client.post("/inbox", data={"title": "Hot take", "body": "agents need evals", "tags": "agents, evals"}, headers=H)
    assert "已保存" in r.text
    (note,) = cfg.resolve_path(cfg.notes.inbox_dir).glob("*.md")
    text = note.read_text()
    assert "tags: [agents, evals]" in text and "# Hot take" in text


def test_approve_uses_next_free_slot(setup):
    client, _llm, engine, ids = setup
    first = client.post(f"/drafts/{ids[0]}/approve", data={"mode": "slot"}, headers=H)
    second = client.post(f"/drafts/{ids[1]}/approve", data={"mode": "slot"}, headers=H)
    a, b = _get(engine, ids[0]).scheduled_at, _get(engine, ids[1]).scheduled_at
    assert "已排期" in first.text and "已排期" in second.text
    assert a != b and {a.hour, b.hour} <= {1, 12}  # 09:00 / 20:00 Asia/Shanghai, one post per slot


def test_suggest_then_write_topic(setup):
    client, llm, engine, _ids = setup
    with Session(engine) as s:
        item_id = s.exec(select(Item)).one().id
    llm.replies.append(SuggestionPlan(topics=[TopicSuggestion(
        title="Agents and plans", angle="A", why_now="W", my_connection="M", format="thread", score=60,
        item_ids=[item_id], trend_terms=["agents"])]))
    r = client.post("/suggest", data={"days": "3"}, headers=H)
    assert r.status_code == 204 and r.headers["X-Redirect"] == "/#topics"
    old = client.get("/topics", follow_redirects=False)
    assert old.status_code == 307 and old.headers["location"] == "/#topics"
    page = client.get("/").text
    assert "生成了 1 个选题" in page  # one-shot flash after the redirect
    assert "Agents and plans" in page and "你的 AI 使用情况" in page
    assert "生成了 1 个选题" not in client.get("/").text
    with Session(engine) as s:
        topic = s.exec(select(Topic).where(Topic.title == "Agents and plans")).one()
    llm.replies += [
        BilingualDraft(zh=PostVersion(tweets=["中文"]), en=PostVersion(tweets=["english"])),
        OptimizedDraft(zh=OptimizedVersion(tweets=["中文终稿"], alt_hooks=["另一个开头"]),
                       en=OptimizedVersion(tweets=["english final"], alt_hooks=["another opening"])),
    ]
    r = client.post(f"/topics/{topic.id}/write", data={"take": "Review is the bottleneck", "format": "quick"}, headers=H)
    assert r.status_code == 204 and r.headers["X-Redirect"] == "/#drafts"
    assert "已写好 2 篇草稿" in client.get("/").text
    with Session(engine) as s:
        en = s.exec(select(Draft).where(Draft.topic_id == topic.id, Draft.lang == "en")).one()
        t = s.get(Topic, topic.id)
        assert t.status == TopicStatus.DRAFTED and t.user_take == "Review is the bottleneck" and t.format == "quick"
    assert "Review is the bottleneck" in llm.calls[-2][1] and "one tweet" in llm.calls[-2][0]
    assert en.stage == "optimized" and en.tweets == ["english final"]
    r = client.post(f"/drafts/{en.id}/hook", data={"index": "0"}, headers=H)
    assert "开头已替换" in r.text
    en = _get(engine, en.id)
    assert en.tweets == ["another opening"] and en.alt_hooks == ["english final"]
    html = client.get("/").text
    assert "其它开头" in html and "历史版本" in html


def test_upload_serve_and_remove_image(setup):
    client, _llm, engine, ids = setup
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    r = client.post(f"/drafts/{ids[0]}/images", data={"tweet": "1"},
                    files={"file": ("shot.png", png, "image/png")}, headers=H)
    assert r.status_code == 200 and "已添加配图" in r.text
    (name,) = _get(engine, ids[0]).images[0]
    assert f"/media/{ids[0]}/{name}" in r.text
    served = client.get(f"/media/{ids[0]}/{name}")
    assert served.status_code == 200 and served.content == png
    assert client.get(f"/media/{ids[0]}/..%2F..%2Fconfig.yaml").status_code == 404
    assert client.get(f"/media/{ids[1]}/{name}").status_code == 404  # belongs to another draft

    r = client.post(f"/drafts/{ids[0]}/images", data={"tweet": "1"},
                    files={"file": ("notes.txt", b"hello", "text/plain")}, headers=H)
    assert "PNG" in r.text and _get(engine, ids[0]).images == [[name]]

    r = client.post(f"/drafts/{ids[0]}/images/remove", data={"tweet": "1", "name": name}, headers=H)
    assert "已删除配图" in r.text and _get(engine, ids[0]).images == [[]]


def test_dismiss_topic(setup):
    client, _llm, engine, _ids = setup
    with Session(engine) as s:
        t = Topic(title="Meh", angle="a", created_at=utcnow())
        s.add(t)
        s.commit()
        tid = t.id
    client.post(f"/topics/{tid}/dismiss", headers=H)
    with Session(engine) as s:
        assert s.get(Topic, tid).status == TopicStatus.DISMISSED
