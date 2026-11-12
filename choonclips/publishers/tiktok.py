"""TikTok via the Content Posting API (Direct Post, FILE_UPLOAD source).

Until TikTok audits your app, direct posts are restricted to private (SELF_ONLY) visibility.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import settings
from .base import TIMEOUT, PostResult, Publisher, PublishError, check, expires, save_account

AUTH = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.publish,video.upload"
CHUNK = 10 * 1024 * 1024


class TikTok(Publisher):
    name = "tiktok"
    label = "TikTok"
    docs = "https://developers.tiktok.com/doc/content-posting-api-get-started"

    def configured(self) -> bool:
        return bool(settings.tiktok_client_key and settings.tiktok_client_secret)

    def auth_url(self, state: str, challenge: str) -> str:
        return AUTH + "?" + urlencode({
            "client_key": settings.tiktok_client_key,
            "response_type": "code",
            "scope": SCOPES,
            "redirect_uri": settings.redirect_uri(self.name),
            "state": state,
        })

    def _token(self, data: dict) -> dict:
        tok = check(httpx.post(
            f"{API}/oauth/token/", data={**data, "client_key": settings.tiktok_client_key,
                                         "client_secret": settings.tiktok_client_secret},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=TIMEOUT,
        ), "TikTok token")
        if "access_token" not in tok:
            raise PublishError(f"TikTok token error: {tok}")
        return tok

    def exchange(self, code: str, verifier: str) -> None:
        tok = self._token({"code": code, "grant_type": "authorization_code",
                           "redirect_uri": settings.redirect_uri(self.name)})
        info = check(httpx.get(
            f"{API}/user/info/", params={"fields": "open_id,display_name,avatar_url"},
            headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=TIMEOUT,
        ), "TikTok user info")
        user = info.get("data", {}).get("user", {})
        save_account(
            self.name, access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
            expires_at=expires(tok.get("expires_in")), account_id=tok.get("open_id"),
            account_name=user.get("display_name", "TikTok account"),
        )

    def refresh(self, acct: dict[str, Any]) -> dict[str, Any]:
        tok = self._token({"grant_type": "refresh_token", "refresh_token": acct["refresh_token"]})
        save_account(self.name, access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
                     expires_at=expires(tok.get("expires_in")))
        return {**acct, "access_token": tok["access_token"]}

    def publish(self, acct: dict[str, Any], video: Path, caption: str, clip: dict[str, Any]) -> PostResult:
        h = {"Authorization": f"Bearer {acct['access_token']}", "Content-Type": "application/json; charset=UTF-8"}
        creator = check(httpx.post(f"{API}/post/publish/creator_info/query/", headers=h, timeout=TIMEOUT),
                        "TikTok creator info").get("data", {})
        options = creator.get("privacy_level_options") or []
        privacy = settings.tiktok_privacy if settings.tiktok_privacy in options or not options else options[-1]

        size = video.stat().st_size
        chunk = size if size <= CHUNK else CHUNK
        count = max(1, math.floor(size / chunk))
        init = check(httpx.post(f"{API}/post/publish/video/init/", headers=h, timeout=TIMEOUT, json={
            "post_info": {
                "title": caption[:2200],
                "privacy_level": privacy,
                "disable_duet": False, "disable_comment": False, "disable_stitch": False,
                "video_cover_timestamp_ms": 1000,
            },
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                            "total_chunk_count": count},
        }), "TikTok post init")
        data = init.get("data", {})
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not upload_url:
            raise PublishError(f"TikTok init returned no upload_url: {init}")

        with video.open("rb") as f:
            for i in range(count):
                start = i * chunk
                # last chunk absorbs the remainder (TikTok rule)
                length = size - start if i == count - 1 else chunk
                f.seek(start)
                body = f.read(length)
                r = httpx.put(upload_url, content=body, timeout=httpx.Timeout(600.0, connect=20.0), headers={
                    "Content-Type": "video/mp4", "Content-Length": str(length),
                    "Content-Range": f"bytes {start}-{start + length - 1}/{size}",
                })
                if r.status_code not in (200, 201, 206):
                    raise PublishError(f"TikTok chunk {i + 1}/{count} failed ({r.status_code}): {r.text[:300]}")

        for _ in range(60):
            st = check(httpx.post(f"{API}/post/publish/status/fetch/", headers=h, timeout=TIMEOUT,
                                  json={"publish_id": publish_id}), "TikTok status").get("data", {})
            status = st.get("status")
            if status == "PUBLISH_COMPLETE":
                ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
                post_id = str(ids[0]) if ids else publish_id
                return PostResult(post_id, "")  # TikTok returns no shareable URL here
            if status == "FAILED":
                raise PublishError(f"TikTok processing failed: {st.get('fail_reason')}")
            time.sleep(5)
        # Still processing: TikTok will finish on its side
        return PostResult(publish_id, "")
