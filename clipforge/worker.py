"""Background work: the video -> clips pipeline, and the posting scheduler.

Runs in daemon threads inside the web server process, so `clipforge` is a single command.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from . import db
from .config import settings
from .pipeline import media, transcribe
from .pipeline.ingest import download_url
from .pipeline.render import render_clip
from .pipeline.scoring import pick_clips
from .pipeline.transcribe import Segment

log = logging.getLogger(__name__)
_wake = threading.Event()
_started = False


def wake() -> None:
    _wake.set()


def _status(video_id: str, status: str, progress: float | None = None, message: str | None = None) -> None:
    fields: dict = {"status": status}
    if progress is not None:
        fields["progress"] = round(progress, 3)
    if message is not None:
        fields["message"] = message
    db.update("videos", video_id, **fields)


def process_video(video_id: str) -> None:
    v = db.one("SELECT * FROM videos WHERE id=?", (video_id,))
    if not v:
        return
    opts = v.get("options") or {}
    work = settings.work_dir / video_id
    work.mkdir(parents=True, exist_ok=True)
    media.require_ffmpeg()

    # 1. Get the file
    src = Path(v["path"]) if v.get("path") else None
    if not src or not src.exists():
        if not v.get("source", "").startswith("http"):
            raise RuntimeError("Source file is missing")
        _status(video_id, "downloading", 0, "Downloading")
        src, title = download_url(v["source"], video_id, lambda p, m: _status(video_id, "downloading", p * 0.15, m))
        db.update("videos", video_id, path=str(src), title=title if v["title"] in ("", v["source"]) else v["title"])
        v = db.one("SELECT * FROM videos WHERE id=?", (video_id,))  # type: ignore[assignment]

    info = media.probe(src)
    db.update("videos", video_id, duration=info["duration"], width=info["width"], height=info["height"])

    # 2. Transcribe
    notes: list[str] = []
    seg_path = work / "transcript.json"
    segments: list[Segment] = []
    if seg_path.exists():
        segments = transcribe.load(seg_path)
    elif info["has_audio"] and transcribe.available():
        _status(video_id, "transcribing", 0.15, "Extracting audio")
        wav = media.extract_audio_wav(src, work / "audio.wav")
        _status(video_id, "transcribing", 0.18, f"Transcribing (whisper {settings.whisper_model})")
        try:
            segments = transcribe.transcribe(
                wav, info["duration"],
                lambda p: _status(video_id, "transcribing", 0.18 + p * 0.42, f"Transcribing {int(p * 100)}%"),
            )
            transcribe.save(segments, seg_path)
        except Exception as e:
            # e.g. the whisper model could not be downloaded: carry on with audio/visual signals only
            log.warning("transcription failed, continuing without transcript: %s", e)
            notes.append(f"Transcription failed ({type(e).__name__}): clips picked without speech, no captions")
        wav.unlink(missing_ok=True)
    elif info["has_audio"]:
        notes.append("faster-whisper not installed: no transcript or captions")

    # 3. Signals
    _status(video_id, "analyzing", 0.62, "Measuring energy and cuts")
    energy = media.audio_rms(src) if info["has_audio"] else np.zeros(0)
    cuts = media.scene_changes(src)

    # 4. Pick clips
    _status(video_id, "analyzing", 0.68, "Finding the best moments")
    clips, strategy = pick_clips(
        segments, energy, cuts, info["duration"], v["title"],
        min_s=opts.get("min_seconds"), max_s=opts.get("max_seconds"), target=opts.get("target_seconds"),
        max_clips=opts.get("max_clips"), use_ai=opts.get("use_ai", True),
    )
    if not clips:
        raise RuntimeError("No usable clips found (is the video long enough?)")

    db.execute("DELETE FROM clips WHERE video_id=?", (video_id,))
    rows = []
    for rank, c in enumerate(clips, 1):
        cid = db.new_id()
        db.insert(
            "clips", id=cid, video_id=video_id, rank=rank, start=c.start, end=c.end, score=c.score,
            title=c.title, hook=c.hook, reason=c.reason, transcript=c.transcript,
            meta={"captions": c.captions, "hashtags": c.hashtags, "signals": c.signals, "strategy": strategy},
            status="pending", created_at=db.now(),
        )
        rows.append((cid, c))

    # 5. Render
    words = [w for s in segments for w in s.words]
    out_dir = settings.clips_dir / video_id
    for i, (cid, c) in enumerate(rows):
        _status(video_id, "rendering", 0.7 + 0.3 * i / len(rows), f"Rendering clip {i + 1}/{len(rows)}")
        db.update("clips", cid, status="rendering")
        out = out_dir / f"{i + 1:02d}_{cid}.mp4"
        try:
            layout = render_clip(
                src, out, c.start, c.end, info["width"], info["height"], words, cuts,
                hook=c.hook if opts.get("hook_overlay", True) else "",
                captions=opts.get("captions", settings.burn_captions),
                layout=opts.get("layout", "auto"), has_audio=info["has_audio"],
            )
            thumb = media.thumbnail(out, min(1.0, (c.end - c.start) / 2), out.with_suffix(".jpg"))
            meta = db.one("SELECT meta FROM clips WHERE id=?", (cid,))["meta"]  # type: ignore[index]
            meta["layout"] = layout
            db.update("clips", cid, status="ready", path=str(out), thumb=str(thumb), meta=meta)
        except Exception as e:
            log.exception("render failed")
            db.update("clips", cid, status="error", error=str(e)[:2000])

    _status(video_id, "ready", 1.0, f"{len(rows)} clips ({strategy})" + "".join(f". {n}" for n in notes))


def _video_loop() -> None:
    while True:
        v = db.one("SELECT id FROM videos WHERE status='queued' ORDER BY created_at LIMIT 1")
        if not v:
            _wake.wait(5)
            _wake.clear()
            continue
        try:
            process_video(v["id"])
        except Exception as e:
            log.error("video %s failed: %s", v["id"], e)
            db.update("videos", v["id"], status="error", error=str(e)[:4000] or traceback.format_exc()[-4000:],
                      message="Failed")


def _post_loop() -> None:
    from .publishers import publish_post

    while True:
        due = db.query(
            "SELECT id FROM posts WHERE status='scheduled' AND scheduled_at<=? ORDER BY scheduled_at LIMIT 5",
            (db.now(),),
        )
        for p in due:
            publish_post(p["id"])
        time.sleep(15)


def recover() -> None:
    """After a restart, put interrupted work back in the queue."""
    db.execute("UPDATE videos SET status='queued' WHERE status IN ('downloading','transcribing','analyzing','rendering')")
    db.execute("UPDATE posts SET status='scheduled' WHERE status='posting'")


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    recover()
    threading.Thread(target=_video_loop, name="clip-worker", daemon=True).start()
    threading.Thread(target=_post_loop, name="post-scheduler", daemon=True).start()
