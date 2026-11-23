"""Minimal X API v2 client: OAuth 2.0 (PKCE, user context), posting, metrics."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from xdraft.config import PublishConfig

SCOPES = "tweet.read tweet.write users.read offline.access media.write"


class XError(RuntimeError):
    pass


class XAuthError(XError):
    """Not connected, or the token was revoked: the user must log in again."""


class XRateLimited(XError):
    def __init__(self, message: str, retry_at: datetime | None):
        super().__init__(message)
        self.retry_at = retry_at


@dataclass
class PendingAuth:
    state: str
    verifier: str


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, token: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(token, fh)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class XClient:
    def __init__(self, cfg: PublishConfig, token_path: Path, http: httpx.Client | None = None):
        self.cfg = cfg
        self.store = TokenStore(token_path)
        self.http = http or httpx.Client(timeout=30.0)
        self.client_id = os.environ.get(cfg.client_id_env, "")
        self.client_secret = os.environ.get(cfg.client_secret_env, "")

    # ---- OAuth 2.0 with PKCE -------------------------------------------------
    @property
    def connected(self) -> bool:
        return bool(self.store.load())

    def start_login(self) -> tuple[str, PendingAuth]:
        if not self.client_id:
            raise XAuthError(f"Set {self.cfg.client_id_env} (from your X developer app) in .env first")
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        url = self.cfg.authorize_url + "?" + urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.cfg.redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return url, PendingAuth(state=state, verifier=verifier)

    def _token_request(self, data: dict) -> dict:
        auth = (self.client_id, self.client_secret) if self.client_secret else None
        if not auth:
            data = {**data, "client_id": self.client_id}
        resp = self.http.post(f"{self.cfg.api_base}/2/oauth2/token", data=data, auth=auth)
        if resp.status_code >= 400:
            raise XAuthError(f"X token request failed ({resp.status_code}): {resp.text[:300]}")
        token = resp.json()
        token["expires_at"] = time.time() + int(token.get("expires_in", 7200))
        self.store.save(token)
        return token

    def finish_login(self, code: str, pending: PendingAuth) -> dict:
        return self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.cfg.redirect_uri,
            "code_verifier": pending.verifier,
        })

    def _refresh(self, token: dict) -> dict:
        if not token.get("refresh_token"):
            raise XAuthError("X session expired; log in again from the dashboard")
        old_refresh = token["refresh_token"]
        new = self._token_request({"grant_type": "refresh_token", "refresh_token": old_refresh})
        new.setdefault("refresh_token", old_refresh)
        self.store.save(new)
        return new

    def _access_token(self) -> str:
        token = self.store.load()
        if not token:
            raise XAuthError("Not connected to X; use “Connect X” in the dashboard")
        if token.get("expires_at", 0) - 60 < time.time():
            token = self._refresh(token)
        return token["access_token"]

    # ---- API calls ---------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            resp = self.http.request(method, f"{self.cfg.api_base}{path}", headers=headers, **kwargs)
            if resp.status_code == 401 and attempt == 0:
                token = self.store.load() or {}
                token["expires_at"] = 0  # force a refresh, then retry once
                self.store.save(token)
                continue
            if resp.status_code == 429:
                reset = resp.headers.get("x-rate-limit-reset")
                retry_at = datetime.fromtimestamp(int(reset), tz=timezone.utc) if reset and reset.isdigit() else None
                raise XRateLimited("X rate limit reached", retry_at)
            if resp.status_code == 401:
                raise XAuthError("X rejected the token; log in again")
            if resp.status_code >= 400:
                raise XError(f"X API {method} {path} failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()
        raise XAuthError("X authentication failed")

    def upload_image(self, name: str, data: bytes, mime: str) -> str:
        """One-request upload (POST /2/media/upload, needs media.write); returns the media id."""
        resp = self._request("POST", "/2/media/upload", files={"media": (name, data, mime)},
                             data={"media_category": "tweet_image"})
        return str(resp["data"]["id"])

    def post_tweet(self, text: str, reply_to: str | None = None, media_ids: list[str] | None = None) -> str:
        body: dict = {"text": text}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        if media_ids:
            body["media"] = {"media_ids": media_ids}
        data = self._request("POST", "/2/tweets", json=body)
        return str(data["data"]["id"])

    def me(self) -> dict:
        return self._request("GET", "/2/users/me").get("data", {})

    def public_metrics(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 100):
            data = self._request("GET", "/2/tweets", params={
                "ids": ",".join(ids[i : i + 100]), "tweet.fields": "public_metrics",
            })
            for tw in data.get("data", []):
                out[str(tw["id"])] = tw.get("public_metrics", {})
        return out
