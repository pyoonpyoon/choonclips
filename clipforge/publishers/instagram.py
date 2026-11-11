"""Instagram Reels via the Instagram API with Instagram Login.

Needs an Instagram professional (Business or Creator) account.
Upload modes:
  * resumable (default): the file is pushed straight from your machine, no public URL needed.
  * url: Instagram downloads the clip from CLIPFORGE_PUBLIC_URL (e.g. a cloudflared/ngrok tunnel to
    this app). Set INSTAGRAM_UPLOAD_MODE=url to use it.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import settings
from .base import TIMEOUT, PostResult, Publisher, PublishError, check, expires, save_account

AUTH = "https://www.instagram.com/oauth/authorize"
TOKEN = "https://api.instagram.com/oauth/access_token"
GRAPH = "https://graph.instagram.com"
VERSION = os.environ.get("INSTAGRAM_API_VERSION", "v23.0")
SCOPES = "instagram_business_basic,instagram_business_content_publish"


class Instagram(Publisher):
    name = "instagram"
    label = "Instagram Reels"
    docs = "https://developers.facebook.com/docs/instagram-platform/content-publishing"

    def configured(self) -> bool:
        return bool(settings.instagram_app_id and settings.instagram_app_secret)

    def auth_url(self, state: str, challenge: str) -> str:
        return AUTH + "?" + urlencode({
            "client_id": settings.instagram_app_id,
            "redirect_uri": settings.redirect_uri(self.name),
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
        })

    def exchange(self, code: str, verifier: str) -> None:
        short = check(httpx.post(TOKEN, data={
            "client_id": settings.instagram_app_id, "client_secret": settings.instagram_app_secret,
            "grant_type": "authorization_code", "redirect_uri": settings.redirect_uri(self.name),
            "code": code.split("#")[0],
        }, timeout=TIMEOUT), "Instagram token exchange")
        long = check(httpx.get(f"{GRAPH}/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": settings.instagram_app_secret,
            "access_token": short["access_token"],
        }, timeout=TIMEOUT), "Instagram long-lived token")
        token = long["access_token"]
        me = check(httpx.get(f"{GRAPH}/{VERSION}/me", params={"fields": "user_id,username", "access_token": token},
                             timeout=TIMEOUT), "Instagram profile")
        save_account(
            self.name, access_token=token, refresh_token=None, expires_at=expires(long.get("expires_in")),
            account_id=str(me.get("user_id") or me.get("id") or short.get("user_id")),
            account_name="@" + me.get("username", "instagram"),
        )

    def refresh(self, acct: dict[str, Any]) -> dict[str, Any]:
        # Long-lived tokens (60 days) are refreshed with themselves while still valid
        tok = check(httpx.get(f"{GRAPH}/refresh_access_token", params={
            "grant_type": "ig_refresh_token", "access_token": acct["access_token"],
        }, timeout=TIMEOUT), "Instagram token refresh (reconnect if expired)")
        save_account(self.name, access_token=tok["access_token"], expires_at=expires(tok.get("expires_in")))
        return {**acct, "access_token": tok["access_token"]}

    def publish(self, acct: dict[str, Any], video: Path, caption: str, clip: dict[str, Any]) -> PostResult:
        token, uid = acct["access_token"], acct["account_id"]
        mode = os.environ.get("INSTAGRAM_UPLOAD_MODE", "resumable")
        params: dict[str, Any] = {"media_type": "REELS", "caption": caption[:2200], "share_to_feed": "true",
                                  "access_token": token}
        if mode == "url":
            public = os.environ.get("CLIPFORGE_PUBLIC_URL", "").rstrip("/")
            if not public:
                raise PublishError("INSTAGRAM_UPLOAD_MODE=url needs CLIPFORGE_PUBLIC_URL")
            params["video_url"] = f"{public}/media/clip/{clip['id']}.mp4"
        else:
            params["upload_type"] = "resumable"
        container = check(httpx.post(f"{GRAPH}/{VERSION}/{uid}/media", data=params, timeout=TIMEOUT),
                          "Instagram container")
        cid = container["id"]

        if mode != "url":
            upload_uri = container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{VERSION}/{cid}"
            size = video.stat().st_size
            with video.open("rb") as f:
                r = httpx.post(upload_uri, content=f.read(), timeout=httpx.Timeout(600.0, connect=20.0), headers={
                    "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
                })
            check(r, "Instagram upload")

        for _ in range(90):
            st = check(httpx.get(f"{GRAPH}/{VERSION}/{cid}", params={"fields": "status_code,status",
                                                                      "access_token": token}, timeout=TIMEOUT),
                       "Instagram status")
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram processing {code}: {st.get('status')}")
            time.sleep(5)
        else:
            raise PublishError("Instagram took too long to process the video")

        pub = check(httpx.post(f"{GRAPH}/{VERSION}/{uid}/media_publish",
                               data={"creation_id": cid, "access_token": token}, timeout=TIMEOUT),
                    "Instagram publish")
        media_id = pub.get("id", "")
        link = ""
        try:
            link = httpx.get(f"{GRAPH}/{VERSION}/{media_id}", params={"fields": "permalink", "access_token": token},
                             timeout=TIMEOUT).json().get("permalink", "")
        except Exception:
            pass
        return PostResult(media_id, link)
