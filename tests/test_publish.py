import json
import os
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlmodel import Session

from xdraft import media
from xdraft.db import get_engine
from xdraft.models import Draft, DraftStatus
from xdraft.publish import service
from xdraft.publish.x import XClient, XRateLimited
from xdraft.timeutil import utcnow


class FakeX:
    """Mock X API: records posts, can fail on the Nth post."""

    def __init__(self, fail_on=None, status=500):
        self.posts, self.fail_on, self.status, self.token_calls = [], fail_on, status, []
        self.uploads = []
        self.revoked = False  # the user revoked xdraft's access on X

    def handler(self, request: httpx.Request):
        if self.revoked:
            return httpx.Response(400 if request.url.path == "/2/oauth2/token" else 401, json={"detail": "revoked"})
        if request.url.path == "/2/media/upload" and request.method == "POST":
            self.uploads.append(request.read())
            return httpx.Response(200, json={"data": {"id": str(900 + len(self.uploads)), "media_key": "3_x"}})
        if request.url.path == "/2/oauth2/token":
            self.token_calls.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"access_token": f"tok{len(self.token_calls)}",
                                             "refresh_token": "ref", "expires_in": 7200})
        if request.url.path == "/2/tweets" and request.method == "POST":
            if self.fail_on is not None and len(self.posts) == self.fail_on:
                self.fail_on = None
                headers = {"x-rate-limit-reset": str(int(time.time()) + 600)} if self.status == 429 else {}
                return httpx.Response(self.status, json={"detail": "boom"}, headers=headers)
            body = json.loads(request.content)
            self.posts.append((body, request.headers["authorization"]))
            return httpx.Response(201, json={"data": {"id": str(100 + len(self.posts)), "text": body["text"]}})
        if request.url.path == "/2/tweets" and request.method == "GET":
            ids = request.url.params["ids"].split(",")
            return httpx.Response(200, json={"data": [
                {"id": i, "public_metrics": {"like_count": 5, "retweet_count": 1, "reply_count": 2}} for i in ids]})
        return httpx.Response(404)


@pytest.fixture
def x_env(cfg, monkeypatch):
    monkeypatch.setenv("X_CLIENT_ID", "cid")
    monkeypatch.delenv("X_CLIENT_SECRET", raising=False)
    fake = FakeX()

    def factory():
        return XClient(cfg.publish, cfg.resolve_path(cfg.publish.token_path),
                       http=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    return fake, factory


def _connect(factory):
    client = factory()
    url, pending = client.start_login()
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"] and q["state"] == [pending.state]
    assert "tweet.write" in q["scope"][0] and "offline.access" in q["scope"][0]
    assert "media.write" in q["scope"][0]  # needed to upload images
    client.finish_login("the-code", pending)
    return client


def _draft(session, tweets, status=DraftStatus.PENDING):
    now = utcnow()
    d = Draft(lang="en", tweets=tweets, status=status, created_at=now, updated_at=now)
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


def test_login_stores_token_privately(cfg, x_env):
    fake, factory = x_env
    _connect(factory)
    path = cfg.resolve_path(cfg.publish.token_path)
    assert path.is_file()
    if os.name != "nt":  # Windows has no POSIX modes; the file lives in the user's own folder
        assert path.stat().st_mode & 0o777 == 0o600
    assert fake.token_calls[0]["code_verifier"] and fake.token_calls[0]["client_id"] == ["cid"]


def test_manual_mode_marks_due_and_never_calls_x(cfg, x_env):
    fake, factory = x_env
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = service.approve(s, _draft(s, ["hello"]))
        service.publish_draft(cfg, s, d, factory)
        assert d.status == DraftStatus.DUE and d.published_at is None and fake.posts == []
    assert '"manual_due"' in cfg.resolve_path("data/publish.log").read_text()


def test_enabled_but_not_connected_is_manual_not_failed(cfg, x_env):
    fake, factory = x_env
    cfg.publish.enabled = True  # the user turned posting on but never connected X
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = service.publish_draft(cfg, s, service.approve(s, _draft(s, ["hello"])), factory)
        assert d.status == DraftStatus.DUE and fake.posts == [] and fake.token_calls == []


def test_mark_posted_parses_link_and_can_be_undone(cfg):
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = _draft(s, ["one"], status=DraftStatus.DUE)
        with pytest.raises(ValueError, match="链接"):
            service.mark_posted(s, d, "https://example.com/nope")
        service.mark_posted(s, d, " https://x.com/someone/status/1234567890123?s=20 ")
        assert d.status == DraftStatus.PUBLISHED and d.tweet_ids == ["1234567890123"] and d.published_at
        service.unmark_posted(s, d)
        assert d.status == DraftStatus.DUE and d.tweet_ids == [] and d.published_at is None
        service.mark_posted(s, d, "")  # the link is optional
        assert d.status == DraftStatus.PUBLISHED and d.tweet_ids == []


def test_edited_too_long_after_approval_is_not_posted(cfg, x_env):
    fake, factory = x_env
    cfg.publish.enabled = True
    _connect(factory)
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = service.approve(s, _draft(s, ["ok", "fine"]))
        d.tweets = ["ok", "x" * 300]  # edited after approval
        s.add(d)
        s.commit()
        service.publish_draft(cfg, s, d, factory)
        assert d.status == DraftStatus.FAILED and "第 2 条" in d.publish_error
    assert fake.posts == []  # nothing half-posted


def test_two_publishers_never_post_the_same_draft_twice(cfg, x_env):
    fake, factory = x_env
    cfg.publish.enabled = True
    _connect(factory)
    engine = get_engine(cfg.resolve_path(cfg.db_path))
    with Session(engine) as s1, Session(engine) as s2:
        d1 = service.approve(s1, _draft(s1, ["once"]))
        d2 = s2.get(Draft, d1.id)  # a second process (scheduled task) read it too
        service.publish_draft(cfg, s1, d1, factory)
        service.publish_draft(cfg, s2, d2, factory)
    assert [p[0]["text"] for p in fake.posts] == ["once"]


def test_revoked_x_token_falls_back_to_manual(cfg, x_env):
    fake, factory = x_env
    cfg.publish.enabled = True
    _connect(factory)
    fake.revoked = True
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = service.publish_draft(cfg, s, service.approve(s, _draft(s, ["hi"])), factory)
        assert d.status == DraftStatus.DUE and fake.posts == []


def test_skip_undo_keeps_the_schedule(cfg):
    from datetime import timedelta as td
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        later = utcnow() + td(hours=3)
        d = service.reject(s, service.approve(s, _draft(s, ["one"]), later))
        assert service.restore(s, d).status == DraftStatus.SCHEDULED
        d2 = _draft(s, ["two"], status=DraftStatus.DUE)
        d2.scheduled_at = utcnow() - td(minutes=5)
        s.add(d2)
        s.commit()
        assert service.restore(s, service.reject(s, d2)).status == DraftStatus.DUE
        assert service.restore(s, service.reject(s, _draft(s, ["three"]))).status == DraftStatus.PENDING


def test_mark_posted_only_from_due(cfg):
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s, pytest.raises(ValueError):
        service.mark_posted(s, _draft(s, ["one"]), "")


def test_rejected_draft_can_be_restored(cfg):
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = service.reject(s, _draft(s, ["one"]))
        assert d.status == DraftStatus.REJECTED
        assert service.restore(s, d).status == DraftStatus.PENDING


def test_unapproved_draft_cannot_be_published(cfg, x_env):
    _fake, factory = x_env
    cfg.publish.enabled = True
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        with pytest.raises(ValueError):
            service.publish_draft(cfg, s, _draft(s, ["x"]), factory)


def test_live_thread_and_metrics(cfg, x_env):
    fake, factory = x_env
    cfg.publish.enabled = True
    _connect(factory)
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = service.approve(s, _draft(s, ["one", "two", "three"]))
        service.publish_draft(cfg, s, d, factory)
        assert d.status == DraftStatus.PUBLISHED and d.tweet_ids == ["101", "102", "103"]
        assert [p[0].get("reply") for p in fake.posts] == [
            None, {"in_reply_to_tweet_id": "101"}, {"in_reply_to_tweet_id": "102"}]
        assert service.refresh_metrics(cfg, s, factory) == 1
        assert d.metrics["like_count"] == 15 and d.metrics["reply_count"] == 6


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_thread_images_are_uploaded_and_attached(cfg, x_env):
    fake, factory = x_env
    cfg.publish.enabled = True
    _connect(factory)
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = _draft(s, ["one", "two", "three"])
        media.save_image(cfg, d, 0, PNG)
        media.save_image(cfg, d, 2, PNG + b"a")
        media.save_image(cfg, d, 2, PNG + b"b")
        s.add(d)
        s.commit()
        service.approve(s, d)
        service.publish_draft(cfg, s, d, factory)
        assert d.status == DraftStatus.PUBLISHED, d.publish_error
    assert len(fake.uploads) == 3
    assert b'name="media_category"' in fake.uploads[0] and b"tweet_image" in fake.uploads[0]
    assert PNG in fake.uploads[0]
    assert [p[0].get("media") for p in fake.posts] == [
        {"media_ids": ["901"]}, None, {"media_ids": ["902", "903"]}]


def test_missing_image_file_fails_the_draft(cfg, x_env):
    fake, factory = x_env
    cfg.publish.enabled = True
    _connect(factory)
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = _draft(s, ["one"])
        name = media.save_image(cfg, d, 0, PNG)
        s.add(d)
        s.commit()
        (media.media_dir(cfg, d.id) / name).unlink()
        service.approve(s, d)
        service.publish_draft(cfg, s, d, factory)
        assert d.status == DraftStatus.FAILED and name in d.publish_error
    assert fake.posts == []


def test_manual_due_logs_images(cfg, x_env):
    _fake, factory = x_env
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = _draft(s, ["one"])
        name = media.save_image(cfg, d, 0, PNG)
        s.add(d)
        s.commit()
        service.publish_draft(cfg, s, service.approve(s, d), factory)
    assert name in cfg.resolve_path("data/publish.log").read_text()


def test_save_image_validates(cfg):
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = _draft(s, ["one", "two"])
        with pytest.raises(ValueError, match="PNG"):
            media.save_image(cfg, d, 0, b"not an image")
        with pytest.raises(ValueError, match="5 MB"):
            media.save_image(cfg, d, 0, PNG + b"\x00" * (5 * 1024 * 1024))
        with pytest.raises(ValueError, match="2"):
            media.save_image(cfg, d, 2, PNG)  # only tweets 1..2 exist
        for _ in range(4):
            media.save_image(cfg, d, 1, PNG)
        with pytest.raises(ValueError, match="4"):
            media.save_image(cfg, d, 1, PNG)
        assert [len(x) for x in media.images_for(d)] == [0, 4]
        gone = d.images[1][0]
        media.remove_image(cfg, d, 1, gone)
        # the file stays so the removal can be undone
        assert gone not in d.images[1] and (media.media_dir(cfg, d.id) / gone).exists()
        media.restore_image(cfg, d, 1, gone)
        assert d.images[1][-1] == gone
        with pytest.raises(ValueError):
            media.restore_image(cfg, d, 1, "../../config.yaml")


def test_failed_thread_resumes_without_duplicates(cfg, x_env):
    fake, factory = x_env
    cfg.publish.enabled = True
    fake.fail_on = 1
    _connect(factory)
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = service.approve(s, _draft(s, ["one", "two"]))
        service.publish_draft(cfg, s, d, factory)
        assert d.status == DraftStatus.FAILED and d.tweet_ids == ["101"] and "500" in d.publish_error
        service.approve(s, d)
        service.publish_draft(cfg, s, d, factory)
        assert d.status == DraftStatus.PUBLISHED and d.tweet_ids == ["101", "102"]
        assert [p[0]["text"] for p in fake.posts] == ["one", "two"]


def test_rate_limit_reschedules(cfg, x_env):
    fake, factory = x_env
    cfg.publish.enabled = True
    fake.fail_on, fake.status = 0, 429
    _connect(factory)
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = service.approve(s, _draft(s, ["one"]))
        service.publish_draft(cfg, s, d, factory)
        assert d.status == DraftStatus.SCHEDULED and d.scheduled_at > utcnow() + timedelta(minutes=9)
        assert service.publish_due(cfg, s, factory) == []  # not due yet


def test_expired_token_is_refreshed(cfg, x_env):
    fake, factory = x_env
    cfg.publish.enabled = True
    client = _connect(factory)
    tok = client.store.load()
    tok["expires_at"] = 0
    client.store.save(tok)
    client.post_tweet("hi")
    assert fake.token_calls[-1]["grant_type"] == ["refresh_token"]
    assert fake.posts[-1][1] == "Bearer tok2"


def test_publish_due_only_publishes_due(cfg, x_env):
    _fake, factory = x_env
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        due = service.approve(s, _draft(s, ["now"]))
        later = service.approve(s, _draft(s, ["later"]), utcnow() + timedelta(hours=2))
        done = service.publish_due(cfg, s, factory)
        assert [d.id for d in done] == [due.id]
        s.refresh(later)
        assert later.status == DraftStatus.SCHEDULED


def test_rate_limited_error_type():
    assert issubclass(XRateLimited, Exception)


def test_slots_respect_timezone_and_capacity(cfg):
    from datetime import datetime, timezone

    cfg.timezone = "Asia/Shanghai"
    cfg.publish.slots = ["09:00", "20:00"]
    after = datetime(2026, 11, 23, 2, 0, tzinfo=timezone.utc)  # 10:00 in Shanghai
    assert [s.hour for s in service.slot_times(cfg, after, days=1)] == [12, 1, 12]
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        first = service.next_free_slot(cfg, s, after)
        assert first == datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        service.approve(s, _draft(s, ["a"]), first)
        assert service.next_free_slot(cfg, s, after) == datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)
