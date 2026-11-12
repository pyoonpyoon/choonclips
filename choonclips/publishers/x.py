"""X (Twitter) via API v2: OAuth 2.0 PKCE, chunked media upload, then a post with the video.

Posting media needs a paid X API tier and the `media.write` scope.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import settings
from .base import TIMEOUT, PostResult, Publisher, PublishError, check, expires, save_account

AUTH = "https://x.com/i/oauth2/authorize"
API = "https://api.x.com/2"
SCOPES = "tweet.read tweet.write users.read media.write offline.access"
CHUNK = 4 * 1024 * 1024


class X(Publisher):
    name = "x"
    label = "X"
    docs = "https://docs.x.com/x-api/media/upload-media"

    def configured(self) -> bool:
        return bool(settings.x_client_id)

    def auth_url(self, state: str, challenge: str) -> str:
        return AUTH + "?" + urlencode({
            "response_type": "code",
            "client_id": settings.x_client_id,
            "redirect_uri": settings.redirect_uri(self.name),
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })

    def _token(self, data: dict) -> dict:
        auth = (settings.x_client_id, settings.x_client_secret) if settings.x_client_secret else None
        if not auth:
            data = {**data, "client_id": settings.x_client_id}
        return check(httpx.post(f"{API}/oauth2/token", data=data, auth=auth, timeout=TIMEOUT), "X token")

    def exchange(self, code: str, verifier: str) -> None:
        tok = self._token({"code": code, "grant_type": "authorization_code",
                           "redirect_uri": settings.redirect_uri(self.name), "code_verifier": verifier})
        me = check(httpx.get(f"{API}/users/me", headers={"Authorization": f"Bearer {tok['access_token']}"},
                             timeout=TIMEOUT), "X profile").get("data", {})
        save_account(
            self.name, access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
            expires_at=expires(tok.get("expires_in")), account_id=me.get("id"),
            account_name="@" + me.get("username", "x"),
        )

    def refresh(self, acct: dict[str, Any]) -> dict[str, Any]:
        if not acct.get("refresh_token"):
            raise PublishError("X session expired: reconnect the account")
        tok = self._token({"grant_type": "refresh_token", "refresh_token": acct["refresh_token"]})
        save_account(self.name, access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
                     expires_at=expires(tok.get("expires_in")))
        return {**acct, "access_token": tok["access_token"]}

    def publish(self, acct: dict[str, Any], video: Path, caption: str, clip: dict[str, Any]) -> PostResult:
        h = {"Authorization": f"Bearer {acct['access_token']}"}
        size = video.stat().st_size
        init = check(httpx.post(f"{API}/media/upload/initialize", headers=h, timeout=TIMEOUT, json={
            "media_type": "video/mp4", "total_bytes": size, "media_category": "tweet_video",
        }), "X media init").get("data", {})
        media_id = init.get("id")
        if not media_id:
            raise PublishError(f"X media init returned no id: {init}")

        with video.open("rb") as f:
            idx = 0
            while chunk := f.read(CHUNK):
                check(httpx.post(f"{API}/media/upload/{media_id}/append", headers=h,
                                 data={"segment_index": str(idx)},
                                 files={"media": ("chunk", chunk, "application/octet-stream")},
                                 timeout=httpx.Timeout(300.0, connect=20.0)), f"X media append {idx}")
                idx += 1

        fin = check(httpx.post(f"{API}/media/upload/{media_id}/finalize", headers=h, timeout=TIMEOUT),
                    "X media finalize").get("data", {})
        info = fin.get("processing_info")
        for _ in range(60):
            if not info or info.get("state") == "succeeded":
                break
            if info.get("state") == "failed":
                raise PublishError(f"X video processing failed: {info.get('error')}")
            time.sleep(max(2, int(info.get("check_after_secs", 3))))
            info = check(httpx.get(f"{API}/media/upload", headers=h, timeout=TIMEOUT,
                                   params={"command": "STATUS", "media_id": media_id}),
                         "X media status").get("data", {}).get("processing_info")

        post = check(httpx.post(f"{API}/tweets", headers=h, timeout=TIMEOUT, json={
            "text": caption[:280], "media": {"media_ids": [str(media_id)]},
        }), "X post").get("data", {})
        pid = post.get("id", "")
        return PostResult(pid, f"https://x.com/i/status/{pid}" if pid else "")
