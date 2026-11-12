"""YouTube Shorts via the YouTube Data API v3 (resumable upload).

A vertical video up to 3 minutes is treated as a Short. Note: videos uploaded through an API project
that has not passed Google's audit are locked to private until the project is verified.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import settings
from .base import TIMEOUT, PostResult, Publisher, PublishError, check, expires, save_account

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


class YouTube(Publisher):
    name = "youtube"
    label = "YouTube Shorts"
    docs = "https://developers.google.com/youtube/v3/guides/uploading_a_video"

    def configured(self) -> bool:
        return bool(settings.youtube_client_id and settings.youtube_client_secret)

    def auth_url(self, state: str, challenge: str) -> str:
        return AUTH + "?" + urlencode({
            "client_id": settings.youtube_client_id,
            "redirect_uri": settings.redirect_uri(self.name),
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })

    def exchange(self, code: str, verifier: str) -> None:
        tok = check(httpx.post(TOKEN, data={
            "code": code, "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "redirect_uri": settings.redirect_uri(self.name), "grant_type": "authorization_code",
            "code_verifier": verifier,
        }, timeout=TIMEOUT), "YouTube token exchange")
        me = check(httpx.get(
            "https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=TIMEOUT,
        ), "YouTube channel lookup")
        ch = (me.get("items") or [{}])[0]
        save_account(
            self.name, access_token=tok["access_token"], refresh_token=tok.get("refresh_token"),
            expires_at=expires(tok.get("expires_in")), account_id=ch.get("id"),
            account_name=ch.get("snippet", {}).get("title", "YouTube channel"),
        )

    def refresh(self, acct: dict[str, Any]) -> dict[str, Any]:
        if not acct.get("refresh_token"):
            raise PublishError("YouTube session expired: reconnect the account")
        tok = check(httpx.post(TOKEN, data={
            "client_id": settings.youtube_client_id, "client_secret": settings.youtube_client_secret,
            "refresh_token": acct["refresh_token"], "grant_type": "refresh_token",
        }, timeout=TIMEOUT), "YouTube token refresh")
        save_account(self.name, access_token=tok["access_token"], expires_at=expires(tok.get("expires_in")))
        return {**acct, "access_token": tok["access_token"]}

    def publish(self, acct: dict[str, Any], video: Path, caption: str, clip: dict[str, Any]) -> PostResult:
        caps = (clip.get("meta") or {}).get("captions", {})
        tags = (clip.get("meta") or {}).get("hashtags", [])
        title = (caps.get("youtube_title") or clip.get("title") or "New short").strip()
        if "#shorts" not in title.lower() and len(title) < 90:
            title += " #shorts"
        body = {
            "snippet": {
                "title": title[:100],
                "description": (caption or caps.get("youtube_description", ""))[:4900],
                "tags": tags[:15],
                "categoryId": "22",
            },
            "status": {"privacyStatus": settings.youtube_privacy, "selfDeclaredMadeForKids": False},
        }
        size = video.stat().st_size
        headers = {"Authorization": f"Bearer {acct['access_token']}"}
        init = httpx.post(
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={**headers, "Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
            content=json.dumps(body), timeout=TIMEOUT,
        )
        check(init, "YouTube upload init")
        upload_url = init.headers.get("Location")
        if not upload_url:
            raise PublishError("YouTube did not return an upload URL")
        with video.open("rb") as f:
            res = httpx.put(upload_url, content=f.read(), headers={**headers, "Content-Type": "video/mp4"},
                            timeout=httpx.Timeout(600.0, connect=20.0))
        data = check(res, "YouTube upload")
        vid = data.get("id", "")
        return PostResult(vid, f"https://youtube.com/shorts/{vid}" if vid else "")
