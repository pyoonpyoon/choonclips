"""Platform registry + the function the scheduler calls to publish one post."""
from __future__ import annotations

import logging
from pathlib import Path

from .. import db
from .base import Publisher, PublishError
from .instagram import Instagram
from .tiktok import TikTok
from .x import X
from .youtube import YouTube

log = logging.getLogger(__name__)

PUBLISHERS: dict[str, Publisher] = {p.name: p for p in (TikTok(), Instagram(), YouTube(), X())}
MAX_ATTEMPTS = 3


def default_caption(platform: str, clip: dict) -> str:
    meta = clip.get("meta") or {}
    caps = meta.get("captions") or {}
    tags = " ".join("#" + t for t in meta.get("hashtags", []))
    if platform == "youtube":
        return caps.get("youtube_description") or f"{clip.get('title', '')}\n{tags}"
    if platform == "x":
        return caps.get("x") or clip.get("hook") or clip.get("title", "")
    text = caps.get(platform) or clip.get("hook") or clip.get("title", "")
    if tags and "#" not in text:
        text = f"{text}\n{tags}"
    return text


def publish_post(post_id: str) -> None:
    post = db.one("SELECT * FROM posts WHERE id=?", (post_id,))
    if not post or post["status"] != "scheduled":
        return
    db.update("posts", post_id, status="posting", attempts=(post.get("attempts") or 0) + 1)
    clip = db.one("SELECT * FROM clips WHERE id=?", (post["clip_id"],))
    pub = PUBLISHERS.get(post["platform"])
    try:
        if not clip or not clip.get("path") or not Path(clip["path"]).exists():
            raise PublishError("Clip file is missing")
        if pub is None:
            raise PublishError(f"Unknown platform {post['platform']}")
        acct = pub.account()
        caption = post.get("caption") or default_caption(pub.name, clip)
        res = pub.publish(acct, Path(clip["path"]), caption, clip)
        db.update("posts", post_id, status="posted", posted_at=db.now(), remote_id=res.remote_id, url=res.url,
                  error=None)
        log.info("posted %s to %s: %s", clip["id"], pub.name, res.url or res.remote_id)
    except Exception as e:
        log.warning("post %s to %s failed: %s", post_id, post["platform"], e)
        attempts = (post.get("attempts") or 0) + 1
        transient = not isinstance(e, PublishError) or "(5" in str(e) or "(429" in str(e)
        if transient and attempts < MAX_ATTEMPTS:
            db.update("posts", post_id, status="scheduled", scheduled_at=db.now() + 120 * attempts, error=str(e)[:2000])
        else:
            db.update("posts", post_id, status="error", error=str(e)[:2000])
