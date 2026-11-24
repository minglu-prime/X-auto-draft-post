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
    assert "Refactors" in html and "四十个文件" in html and "手动发布" in html
    assert "X 未配置" not in html  # useless in manual mode
    for page in ("/sources", "/inbox", "/history"):
        assert client.get(page).status_code == 200


def test_stepper_points_to_publish_when_a_post_is_due(setup):
    client, _llm, _engine, ids = setup
    client.post(f"/drafts/{ids[0]}/approve", data={"mode": "now"}, headers=H)
    html = client.get("/").text
    assert 'data-current="publish"' in html and "该发了" in html


def test_old_dry_runs_become_due_on_start(cfg, setup):
    _client, _llm, engine, ids = setup
    with Session(engine) as s:
        d = s.get(Draft, ids[0])
        d.status = "dry_run"  # never really posted: older versions called this a dry run
        s.add(d)
        s.commit()
    create_app(cfg, llm_factory=FakeLLM, start_scheduler=False, allowed_hosts=("testserver",))
    assert _get(engine, ids[0]).status == DraftStatus.DUE


def test_autosave_returns_only_the_meta_fragment(setup):
    client, _llm, engine, ids = setup
    r = client.post(f"/drafts/{ids[1]}/save", data={"body": "first 99% claim\n---\nsecond"},
                    headers={**H, "X-Autosave": "1"})
    assert r.status_code == 200 and "<article" not in r.text and "<textarea" not in r.text
    assert f'id="meta-{ids[1]}"' in r.text and "99%" in r.text
    assert _get(engine, ids[1]).tweets == ["first 99% claim", "second"]


def test_emptying_a_tweet_with_images_is_refused(setup):
    client, _llm, engine, ids = setup
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    client.post(f"/drafts/{ids[0]}/save", data={"tweet": ["a", "b"], "src": ["0", "-1"]}, headers=H)
    client.post(f"/drafts/{ids[0]}/images", data={"tweet": "2"}, files={"file": ("p.png", png, "image/png")}, headers=H)
    r = client.post(f"/drafts/{ids[0]}/save", data={"tweet": ["a", ""], "src": ["0", "1"]},
                    headers={**H, "X-Autosave": "1"})
    assert "配图" in r.text
    d = _get(engine, ids[0])
    assert d.tweets == ["a", "b"] and len(d.images[1]) == 1


def test_no_edits_once_posting_started(setup):
    client, _llm, engine, ids = setup
    with Session(engine) as s:
        d = s.get(Draft, ids[0])
        d.status, d.tweet_ids = DraftStatus.FAILED, ["111"]  # first tweet of the thread is already on X
        s.add(d)
        s.commit()
    r = client.post(f"/drafts/{ids[0]}/save", data={"tweet": ["changed"], "src": ["0"]},
                    headers={**H, "X-Autosave": "1"})
    assert "已经发出" in r.text and _get(engine, ids[0]).tweets == ["四十个文件的重构，计划没跑偏。"]
    assert "<textarea" not in client.get("/").text.split(f'id="draft-{ids[0]}"')[1].split("</article>")[0]


def test_status_reports_counts(setup):
    client, _llm, _engine, ids = setup
    assert client.get("/status").json() == {"due": 0, "review": 2, "scheduled": 0}
    client.post(f"/drafts/{ids[0]}/approve", data={"mode": "now"}, headers=H)
    assert client.get("/status").json()["due"] == 1


def test_due_card_offers_copy_and_mark_posted_with_undo(setup):
    client, _llm, engine, ids = setup
    r = client.post(f"/drafts/{ids[0]}/approve", data={"mode": "now"}, headers=H)
    assert "data-copy=" in r.text and "我已发出" in r.text and "x.com/compose/post" in r.text
    r = client.post(f"/drafts/{ids[0]}/posted", data={"url": "https://x.com/me/status/42"}, headers=H)
    assert f'data-undo="/drafts/{ids[0]}/unposted"' in r.text
    assert _get(engine, ids[0]).status == DraftStatus.PUBLISHED and _get(engine, ids[0]).tweet_ids == ["42"]
    assert "x.com/i/web/status/42" in client.get("/history").text
    client.post(f"/drafts/{ids[0]}/unposted", headers=H)
    assert _get(engine, ids[0]).status == DraftStatus.DUE
    r = client.post(f"/drafts/{ids[0]}/posted", data={"url": "not a link"}, headers=H)
    assert "链接" in r.text and _get(engine, ids[0]).status == DraftStatus.DUE


def test_suggest_collects_first_when_material_is_stale(setup, monkeypatch):
    client, llm, engine, _ids = setup
    import xdraft.web.app as web_app
    calls = []
    monkeypatch.setattr(web_app, "run_collect", lambda cfg, since: calls.append(since) or [])
    llm.replies.append(SuggestionPlan(topics=[]))
    client.post("/suggest", data={"days": "3"}, headers=H)
    assert calls == []  # material was collected just now
    with Session(engine) as s:
        for it in s.exec(select(Item)).all():
            it.collected_at = utcnow() - timedelta(hours=3)
            s.add(it)
        s.commit()
    llm.replies.append(SuggestionPlan(topics=[]))
    client.post("/suggest", data={"days": "3"}, headers=H)
    assert len(calls) == 1


def test_warnings_are_shown_in_chinese():
    from xdraft.web.app import warn_zh
    assert warn_zh('AI-ish: "1/7"') == "像 AI 写的：「1/7」"
    assert warn_zh("Tweet 2 is 301/280 weighted characters") == "第 2 条 301/280，超长了"
    assert warn_zh("Number “40%” does not appear in the sources — verify it") == "数字「40%」在素材里找不到，核对一下"
    assert warn_zh("something new") == "something new"


def test_save_splits_thread_and_rechecks(setup):
    client, _llm, engine, ids = setup
    r = client.post(f"/drafts/{ids[1]}/save", data={"body": "first 99% claim\n---\nsecond"}, headers=H)
    assert r.status_code == 200 and "已保存" in r.text
    d = _get(engine, ids[1])
    assert d.tweets == ["first 99% claim", "second"] and d.edited
    assert any("99%" in w for w in d.warnings)


def test_post_now_in_manual_mode_is_due(setup):
    client, _llm, engine, ids = setup
    r = client.post(f"/drafts/{ids[0]}/approve", data={"mode": "now"}, headers=H)
    assert "该发了" in r.text
    assert _get(engine, ids[0]).status == DraftStatus.DUE


def test_schedule_converts_local_time(setup):
    client, _llm, engine, ids = setup
    # UTC+8 browser: getTimezoneOffset() = -480
    r = client.post(f"/drafts/{ids[0]}/approve",
                    data={"mode": "custom", "when": "2030-01-02T09:30", "tz_offset": "-480"}, headers=H)
    assert "已排期：周三 01-02 09:30" in r.text  # read in the configured timezone, not the browser's
    assert _get(engine, ids[0]).scheduled_at.hour == 1  # stored as 01:30 UTC
    client.post(f"/drafts/{ids[0]}/unschedule", headers=H)
    assert _get(engine, ids[0]).status == DraftStatus.PENDING


def test_reject_hides_card_and_can_be_undone(setup):
    client, _llm, engine, ids = setup
    r = client.post(f"/drafts/{ids[0]}/reject", headers=H)
    assert _get(engine, ids[0]).status == DraftStatus.REJECTED
    assert f'id="draft-{ids[0]}"' in r.text and "<textarea" not in r.text  # placeholder, not the card
    assert f'data-undo="/drafts/{ids[0]}/restore"' in r.text
    r = client.post(f"/drafts/{ids[0]}/restore", headers=H)
    assert "<textarea" in r.text and _get(engine, ids[0]).status == DraftStatus.PENDING


def test_approve_offers_undo(setup):
    client, _llm, _engine, ids = setup
    r = client.post(f"/drafts/{ids[0]}/approve", data={"mode": "slot"}, headers=H)
    assert f'data-undo="/drafts/{ids[0]}/unschedule"' in r.text


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
    assert "已生成 1 个选题" in page  # one-shot flash after the redirect
    assert "Agents and plans" in page and "你的 AI 使用情况" in page
    assert "已生成 1 个选题" not in client.get("/").text
    with Session(engine) as s:
        topic = s.exec(select(Topic).where(Topic.title == "Agents and plans")).one()
    llm.replies += [
        BilingualDraft(zh=PostVersion(tweets=["中文"]), en=PostVersion(tweets=["english"])),
        OptimizedDraft(zh=OptimizedVersion(tweets=["中文终稿"], alt_hooks=["另一个开头"]),
                       en=OptimizedVersion(tweets=["english final"], alt_hooks=["another opening"])),
    ]
    r = client.post(f"/topics/{topic.id}/write", data={"take": "Review is the bottleneck", "format": "quick"}, headers=H)
    assert r.status_code == 204 and r.headers["X-Redirect"] == "/#drafts"
    assert "中英草稿已生成（2 篇）" in client.get("/").text
    with Session(engine) as s:
        en = s.exec(select(Draft).where(Draft.topic_id == topic.id, Draft.lang == "en")).one()
        t = s.get(Topic, topic.id)
        assert t.status == TopicStatus.DRAFTED and t.user_take == "Review is the bottleneck" and t.format == "quick"
    assert "Review is the bottleneck" in llm.calls[-2][1] and "one tweet" in llm.calls[-2][0]
    assert en.stage == "optimized" and en.tweets == ["english final"]
    r = client.post(f"/drafts/{en.id}/hook", data={"index": "0"}, headers=H)
    assert "已换开头" in r.text
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
    assert f'data-undo="/drafts/{ids[0]}/images/restore?tweet=1&amp;name={name}"' in r.text
    r = client.post(f"/drafts/{ids[0]}/images/restore?tweet=1&name={name}", headers=H)
    assert r.status_code == 200 and _get(engine, ids[0]).images == [[name]]


def test_paste_upload_needs_no_tweet_number_for_single_tweet(setup):
    client, _llm, engine, ids = setup
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    r = client.post(f"/drafts/{ids[0]}/images", files={"file": ("p.png", png, "image/png")}, headers=H)
    assert r.status_code == 200 and len(_get(engine, ids[0]).images[0]) == 1


def test_skip_topic_can_be_undone(setup):
    client, _llm, engine, _ids = setup
    with Session(engine) as s:
        t = Topic(title="Meh", angle="a", created_at=utcnow())
        s.add(t)
        s.commit()
        tid = t.id
    r = client.post(f"/topics/{tid}/dismiss", headers=H)
    assert f'id="topic-{tid}"' in r.text and f'data-undo="/topics/{tid}/restore"' in r.text
    with Session(engine) as s:
        assert s.get(Topic, tid).status == TopicStatus.DISMISSED
    r = client.post(f"/topics/{tid}/restore", headers=H)
    assert "Meh" in r.text
    with Session(engine) as s:
        assert s.get(Topic, tid).status == TopicStatus.SUGGESTED
