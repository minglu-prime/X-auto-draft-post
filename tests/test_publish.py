import json
import os
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlmodel import Session

from xdraft.db import get_engine
from xdraft.models import Draft, DraftStatus
from xdraft.publish import service
from xdraft.publish.x import XClient, XRateLimited
from xdraft.timeutil import utcnow


class FakeX:
    """Mock X API: records posts, can fail on the Nth post."""

    def __init__(self, fail_on=None, status=500):
        self.posts, self.fail_on, self.status, self.token_calls = [], fail_on, status, []

    def handler(self, request: httpx.Request):
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


def test_dry_run_never_calls_x(cfg, x_env):
    fake, factory = x_env
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        d = service.approve(s, _draft(s, ["hello"]))
        service.publish_draft(cfg, s, d, factory)
        assert d.status == DraftStatus.DRY_RUN and fake.posts == []
    assert "dry_run" in cfg.resolve_path("data/publish.log").read_text()


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
