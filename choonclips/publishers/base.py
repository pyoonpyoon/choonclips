"""Shared bits for platform publishers."""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .. import db

TIMEOUT = httpx.Timeout(60.0, connect=20.0)


class PublishError(RuntimeError):
    pass


@dataclass
class PostResult:
    remote_id: str
    url: str = ""


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def check(resp: httpx.Response, what: str) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise PublishError(f"{what} failed ({resp.status_code}): {resp.text[:800]}")
    try:
        return resp.json() if resp.content else {}
    except ValueError:
        return {}


def save_account(platform: str, **fields: Any) -> None:
    existing = db.one("SELECT platform FROM accounts WHERE platform=?", (platform,))
    if existing:
        db.update("accounts", platform, key="platform", **fields)
    else:
        db.insert("accounts", platform=platform, connected_at=db.now(), **fields)


def get_account(platform: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM accounts WHERE platform=?", (platform,))


def expires(seconds: Any) -> float | None:
    try:
        return time.time() + float(seconds) - 60
    except (TypeError, ValueError):
        return None


def needs_refresh(acct: dict[str, Any]) -> bool:
    exp = acct.get("expires_at")
    return bool(exp) and exp < time.time()


class Publisher:
    name: str = ""
    label: str = ""
    docs: str = ""

    def configured(self) -> bool:
        raise NotImplementedError

    def auth_url(self, state: str, challenge: str) -> str:
        raise NotImplementedError

    def exchange(self, code: str, verifier: str) -> None:
        """Swap an auth code for tokens and save the account."""
        raise NotImplementedError

    def refresh(self, acct: dict[str, Any]) -> dict[str, Any]:
        return acct

    def publish(self, acct: dict[str, Any], video: Path, caption: str, clip: dict[str, Any]) -> PostResult:
        raise NotImplementedError

    # helpers
    def account(self) -> dict[str, Any]:
        acct = get_account(self.name)
        if not acct or not acct.get("access_token"):
            raise PublishError(f"{self.label} is not connected")
        if needs_refresh(acct):
            acct = self.refresh(acct)
        return acct
