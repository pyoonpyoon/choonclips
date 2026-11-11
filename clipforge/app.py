"""Web app: REST API + the single-page studio UI."""
from __future__ import annotations

import io
import json
import logging
import secrets
import shutil
import threading
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, worker
from .config import settings
from .pipeline import media, reframe, transcribe
from .pipeline.render import has_subtitles, render_clip
from .pipeline.scoring import claude_available
from .publishers import PUBLISHERS, default_caption, publish_post
from .publishers.base import get_account, pkce_pair

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}



@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.ensure_dirs()
    db.connect()
    worker.start()
    yield


app = FastAPI(title="ClipForge", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------- pages ----------

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text()


# ---------- status ----------

def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


@app.get("/api/status")
def status() -> dict[str, Any]:
    platforms = []
    for p in PUBLISHERS.values():
        acct = get_account(p.name)
        platforms.append({
            "name": p.name, "label": p.label, "configured": p.configured(), "docs": p.docs,
            "connected": bool(acct and acct.get("access_token")),
            "account_name": acct.get("account_name") if acct else None,
            "redirect_uri": settings.redirect_uri(p.name),
        })
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    return {
        "capabilities": {
            "ffmpeg": ffmpeg_ok,
            "captions": ffmpeg_ok and has_subtitles(),
            "transcribe": transcribe.available(),
            "face_reframe": reframe.available(),
            "claude": claude_available(),
            "url_import": _has("yt_dlp"),
        },
        "defaults": {
            "min_seconds": settings.min_clip_seconds, "max_seconds": settings.max_clip_seconds,
            "target_seconds": settings.target_clip_seconds, "max_clips": settings.max_clips,
        },
        "platforms": platforms,
    }


# ---------- videos ----------

def _options(raw: str | dict | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        opts = raw
    else:
        try:
            opts = json.loads(raw or "{}")
        except json.JSONDecodeError:
            opts = {}
    clean: dict[str, Any] = {}
    for k in ("min_seconds", "max_seconds", "target_seconds", "max_clips"):
        if opts.get(k) not in (None, ""):
            clean[k] = int(opts[k])
    for k in ("use_ai", "captions", "hook_overlay"):
        if k in opts:
            clean[k] = bool(opts[k])
    if opts.get("layout") in ("auto", "crop", "fit", "fill"):
        clean["layout"] = opts["layout"]
    return clean


@app.post("/api/videos")
async def upload_video(file: UploadFile = File(...), options: str = Form("{}")) -> dict[str, Any]:
    ext = Path(file.filename or "video.mp4").suffix.lower()
    if ext not in VIDEO_EXT:
        raise HTTPException(400, f"Unsupported file type {ext}")
    vid = db.new_id()
    dest_dir = settings.uploads_dir / vid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"source{ext}"
    with dest.open("wb") as out:
        while chunk := await file.read(8 * 1024 * 1024):
            out.write(chunk)
    title = Path(file.filename or "Untitled").stem
    db.insert("videos", id=vid, title=title, source=file.filename, path=str(dest), status="queued",
              options=_options(options), created_at=db.now(), message="Queued")
    worker.wake()
    return {"id": vid}


class UrlIn(BaseModel):
    url: str
    options: dict[str, Any] = {}


@app.post("/api/videos/url")
def import_url(body: UrlIn) -> dict[str, Any]:
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "Enter a full http(s) URL")
    vid = db.new_id()
    db.insert("videos", id=vid, title=body.url, source=body.url, status="queued",
              options=_options(body.options), created_at=db.now(), message="Queued")
    worker.wake()
    return {"id": vid}


def _clip_out(c: dict[str, Any]) -> dict[str, Any]:
    c = dict(c)
    c["video_url"] = f"/media/clip/{c['id']}.mp4" if c.get("path") else None
    c["thumb_url"] = f"/media/thumb/{c['id']}.jpg" if c.get("thumb") else None
    c.pop("path", None)
    c.pop("thumb", None)
    c["posts"] = db.query("SELECT * FROM posts WHERE clip_id=? ORDER BY scheduled_at", (c["id"],))
    return c


@app.get("/api/videos")
def list_videos() -> list[dict[str, Any]]:
    vids = db.query("SELECT * FROM videos ORDER BY created_at DESC")
    for v in vids:
        v["clip_count"] = (db.one("SELECT COUNT(*) n FROM clips WHERE video_id=?", (v["id"],)) or {}).get("n", 0)
        first = db.one("SELECT id FROM clips WHERE video_id=? AND thumb IS NOT NULL ORDER BY rank LIMIT 1", (v["id"],))
        v["thumb_url"] = f"/media/thumb/{first['id']}.jpg" if first else None
        v.pop("path", None)
    return vids


@app.get("/api/videos/{vid}")
def get_video(vid: str) -> dict[str, Any]:
    v = db.one("SELECT * FROM videos WHERE id=?", (vid,))
    if not v:
        raise HTTPException(404)
    v.pop("path", None)
    v["clips"] = [_clip_out(c) for c in db.query("SELECT * FROM clips WHERE video_id=? ORDER BY rank", (vid,))]
    return v


@app.post("/api/videos/{vid}/reprocess")
def reprocess(vid: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    v = db.one("SELECT * FROM videos WHERE id=?", (vid,))
    if not v:
        raise HTTPException(404)
    opts = {**(v.get("options") or {}), **_options(body or {})}
    db.update("videos", vid, status="queued", error=None, progress=0, message="Queued", options=opts)
    worker.wake()
    return {"ok": True}


@app.delete("/api/videos/{vid}")
def delete_video(vid: str) -> dict[str, Any]:
    db.execute("DELETE FROM videos WHERE id=?", (vid,))
    for d in (settings.uploads_dir / vid, settings.work_dir / vid, settings.clips_dir / vid):
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


# ---------- clips ----------

class ClipPatch(BaseModel):
    title: str | None = None
    hook: str | None = None
    captions: dict[str, str] | None = None
    hashtags: list[str] | None = None
    start: float | None = None
    end: float | None = None


def _get_clip(cid: str) -> dict[str, Any]:
    c = db.one("SELECT * FROM clips WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404)
    return c


@app.patch("/api/clips/{cid}")
def patch_clip(cid: str, body: ClipPatch) -> dict[str, Any]:
    c = _get_clip(cid)
    meta = c.get("meta") or {}
    fields: dict[str, Any] = {}
    if body.title is not None:
        fields["title"] = body.title
    if body.hook is not None:
        fields["hook"] = body.hook
    if body.captions is not None:
        meta["captions"] = {**meta.get("captions", {}), **body.captions}
    if body.hashtags is not None:
        meta["hashtags"] = [h.lstrip("#").strip() for h in body.hashtags if h.strip()]
    fields["meta"] = meta
    retime = (body.start is not None and abs(body.start - c["start"]) > 0.01) or (
        body.end is not None and abs(body.end - c["end"]) > 0.01)
    hook_changed = body.hook is not None and body.hook != c.get("hook")
    if retime:
        fields["start"] = max(0.0, body.start if body.start is not None else c["start"])
        fields["end"] = body.end if body.end is not None else c["end"]
        if fields["end"] - fields["start"] < 1:
            raise HTTPException(400, "Clip must be at least 1 second")
    db.update("clips", cid, **fields)
    if retime or hook_changed:
        _rerender_async(cid)
    return _clip_out(_get_clip(cid))


def _rerender(cid: str) -> None:
    c = _get_clip(cid)
    v = db.one("SELECT * FROM videos WHERE id=?", (c["video_id"],))
    if not v or not v.get("path"):
        return
    opts = v.get("options") or {}
    db.update("clips", cid, status="rendering")
    try:
        src = Path(v["path"])
        info = media.probe(src)
        seg_path = settings.work_dir / v["id"] / "transcript.json"
        words = [w for s in transcribe.load(seg_path) for w in s.words] if seg_path.exists() else []
        out = Path(c["path"]) if c.get("path") else settings.clips_dir / v["id"] / f"{c['rank']:02d}_{cid}.mp4"
        tmp = out.with_name(out.stem + "_new.mp4")
        cuts = media.scene_changes(src)
        render_clip(src, tmp, c["start"], c["end"], info["width"], info["height"], words, cuts,
                    hook=(c.get("hook") or "") if opts.get("hook_overlay", True) else "",
                    captions=opts.get("captions", settings.burn_captions), layout=opts.get("layout", "auto"),
                    has_audio=info["has_audio"])
        tmp.replace(out)
        thumb = media.thumbnail(out, min(1.0, (c["end"] - c["start"]) / 2), out.with_suffix(".jpg"))
        db.update("clips", cid, status="ready", path=str(out), thumb=str(thumb), error=None)
    except Exception as e:
        log.exception("re-render failed")
        db.update("clips", cid, status="error", error=str(e)[:2000])


def _rerender_async(cid: str) -> None:
    threading.Thread(target=_rerender, args=(cid,), daemon=True).start()


@app.delete("/api/clips/{cid}")
def delete_clip(cid: str) -> dict[str, Any]:
    c = _get_clip(cid)
    for k in ("path", "thumb"):
        if c.get(k):
            Path(c[k]).unlink(missing_ok=True)
    db.execute("DELETE FROM clips WHERE id=?", (cid,))
    return {"ok": True}


@app.get("/media/clip/{cid}.mp4")
def clip_file(cid: str) -> FileResponse:
    c = _get_clip(cid)
    if not c.get("path") or not Path(c["path"]).exists():
        raise HTTPException(404)
    return FileResponse(c["path"], media_type="video/mp4", filename=f"clip_{c['rank']:02d}.mp4",
                        content_disposition_type="inline")


@app.get("/media/thumb/{cid}.jpg")
def clip_thumb(cid: str) -> FileResponse:
    c = _get_clip(cid)
    if not c.get("thumb") or not Path(c["thumb"]).exists():
        raise HTTPException(404)
    return FileResponse(c["thumb"], media_type="image/jpeg")


@app.get("/api/clips/{cid}/package")
def clip_package(cid: str) -> StreamingResponse:
    """Zip with the video and ready-to-paste copy for every platform (for manual posting)."""
    c = _get_clip(cid)
    if not c.get("path") or not Path(c["path"]).exists():
        raise HTTPException(404, "Clip not rendered")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.write(c["path"], "clip.mp4")
        if c.get("thumb") and Path(c["thumb"]).exists():
            z.write(c["thumb"], "cover.jpg")
        meta = c.get("meta") or {}
        caps = meta.get("captions", {})
        txt = [f"TITLE: {c.get('title', '')}", f"HOOK: {c.get('hook', '')}", ""]
        for name in ("tiktok", "instagram", "x"):
            txt += [f"--- {name.upper()} ---", default_caption(name, c), ""]
        txt += ["--- YOUTUBE SHORTS ---", f"Title: {caps.get('youtube_title', c.get('title', ''))}",
                default_caption("youtube", c), "", "Hashtags: " + " ".join("#" + h for h in meta.get("hashtags", []))]
        z.writestr("captions.txt", "\n".join(txt))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="clip_{c["rank"]:02d}.zip"'})


# ---------- posting ----------

class PostIn(BaseModel):
    clip_ids: list[str]
    platforms: list[str]
    start_at: float | None = None        # unix seconds; None = now
    interval_minutes: int = 0             # spacing between clips (0 = all at once)
    captions: dict[str, dict[str, str]] = {}   # clip_id -> platform -> caption override


@app.post("/api/posts")
def create_posts(body: PostIn) -> dict[str, Any]:
    bad = [p for p in body.platforms if p not in PUBLISHERS]
    if bad:
        raise HTTPException(400, f"Unknown platform(s): {bad}")
    not_connected = [PUBLISHERS[p].label for p in body.platforms if not (get_account(p) or {}).get("access_token")]
    if not_connected:
        raise HTTPException(400, "Connect these accounts first: " + ", ".join(not_connected))
    t0 = body.start_at or db.now()
    created = []
    for i, cid in enumerate(body.clip_ids):
        clip = _get_clip(cid)
        if clip["status"] != "ready":
            raise HTTPException(400, f"Clip {clip['rank']} is not rendered yet")
        when = t0 + i * body.interval_minutes * 60
        for p in body.platforms:
            pid = db.new_id()
            caption = body.captions.get(cid, {}).get(p) or default_caption(p, clip)
            db.insert("posts", id=pid, clip_id=cid, platform=p, caption=caption, status="scheduled",
                      scheduled_at=when, created_at=db.now())
            created.append(pid)
    if body.start_at is None and body.interval_minutes == 0:
        for pid in created:
            threading.Thread(target=publish_post, args=(pid,), daemon=True).start()
    return {"created": created}


@app.get("/api/posts")
def list_posts() -> list[dict[str, Any]]:
    return db.query(
        "SELECT posts.*, clips.title AS clip_title, clips.video_id FROM posts "
        "JOIN clips ON clips.id = posts.clip_id ORDER BY posts.scheduled_at DESC LIMIT 500")


@app.post("/api/posts/{pid}/cancel")
def cancel_post(pid: str) -> dict[str, Any]:
    db.execute("UPDATE posts SET status='canceled' WHERE id=? AND status='scheduled'", (pid,))
    return {"ok": True}


@app.post("/api/posts/{pid}/retry")
def retry_post(pid: str) -> dict[str, Any]:
    db.execute("UPDATE posts SET status='scheduled', scheduled_at=?, attempts=0, error=NULL WHERE id=? "
               "AND status IN ('error','canceled')", (db.now(), pid))
    return {"ok": True}


# ---------- OAuth ----------

@app.get("/oauth/{platform}/start")
def oauth_start(platform: str) -> RedirectResponse:
    pub = PUBLISHERS.get(platform)
    if not pub:
        raise HTTPException(404)
    if not pub.configured():
        raise HTTPException(400, f"{pub.label} app credentials are not set. See README > Connecting accounts.")
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    db.execute("DELETE FROM oauth_states WHERE created_at < ?", (db.now() - 3600,))
    db.insert("oauth_states", state=state, platform=platform, verifier=verifier, created_at=db.now())
    return RedirectResponse(pub.auth_url(state, challenge))


@app.get("/oauth/{platform}/callback")
def oauth_callback(platform: str, request: Request) -> RedirectResponse:
    q = request.query_params
    if q.get("error"):
        return RedirectResponse(f"/?connect_error={platform}:{q.get('error_description') or q.get('error')}")
    st = db.one("SELECT * FROM oauth_states WHERE state=? AND platform=?", (q.get("state", ""), platform))
    if not st:
        raise HTTPException(400, "Invalid or expired OAuth state; start the connection again")
    db.execute("DELETE FROM oauth_states WHERE state=?", (st["state"],))
    pub = PUBLISHERS[platform]
    try:
        pub.exchange(q.get("code", ""), st["verifier"])
    except Exception as e:
        log.exception("oauth exchange failed")
        return RedirectResponse(f"/?connect_error={platform}:{str(e)[:300]}")
    return RedirectResponse(f"/?connected={platform}")


@app.post("/api/accounts/{platform}/disconnect")
def disconnect(platform: str) -> dict[str, Any]:
    db.execute("DELETE FROM accounts WHERE platform=?", (platform,))
    return {"ok": True}
