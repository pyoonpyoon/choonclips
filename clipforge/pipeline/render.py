"""Render a clip: cut, reframe to 1080x1920, burn captions, encode for every platform at once.

Output: H.264 High / yuv420p / 30 fps / AAC 48 kHz / +faststart. That single file is accepted by
TikTok, Instagram Reels, YouTube Shorts and X.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from . import reframe
from .captions import build_ass
from .media import MediaError
from .transcribe import Word

log = logging.getLogger(__name__)
OUT_W, OUT_H = 1080, 1920


def _has_subtitles_filter() -> bool:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    return " subtitles " in proc.stdout


_SUBS = None


def has_subtitles() -> bool:
    global _SUBS
    if _SUBS is None:
        _SUBS = _has_subtitles_filter()
    return _SUBS


def video_filter(
    src: Path,
    start: float,
    end: float,
    src_w: int,
    src_h: int,
    cuts_rel: list[float],
    layout: str = "auto",
) -> tuple[str, str]:
    """Returns (filter_complex producing [v0], layout actually used)."""
    vertical_already = src_h >= src_w * 1.2
    crop_w = int(src_h * 9 / 16) // 2 * 2

    if vertical_already or layout == "fill":
        return (f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUT_W}:{OUT_H},setsar=1,fps=30[v0]"), "fill"

    if layout in ("auto", "crop") and crop_w < src_w:
        plan = None
        if reframe.available():
            try:
                samples = reframe.face_track(src, start, end)
                plan = reframe.plan_crop(samples, cuts_rel, end - start)
            except Exception as e:  # pragma: no cover
                log.warning("face tracking failed: %s", e)
        if plan is not None or layout == "crop":
            x = reframe.crop_x_expr(plan, src_w, crop_w) if plan else f"(iw-{crop_w})/2"
            return (f"[0:v]crop={crop_w}:{src_h}:{x}:0,scale={OUT_W}:{OUT_H},setsar=1,fps=30[v0]"), "crop"

    # "fit": whole frame on a blurred, zoomed copy of itself
    return (
        f"[0:v]split=2[a][b];"
        f"[a]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},boxblur=24:2,eq=brightness=-0.08[bg];"
        f"[b]scale={OUT_W}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30[v0]"
    ), "fit"


def render_clip(
    src: Path,
    out: Path,
    start: float,
    end: float,
    src_w: int,
    src_h: int,
    words: list[Word],
    cuts: list[float],
    hook: str = "",
    captions: bool = True,
    layout: str = "auto",
    has_audio: bool = True,
) -> str:
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = end - start
    cuts_rel = [c - start for c in cuts if start < c < end]
    fc, used = video_filter(src, start, end, src_w, src_h, cuts_rel, layout)
    last = "[v0]"
    if captions and has_subtitles():
        ass = out.with_suffix(".ass")
        ass.write_text(build_ass(words, start, end, hook=hook), encoding="utf-8")
        fc += f";[v0]subtitles={ass.name}[v1]"
        last = "[v1]"

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src.resolve()),
        "-filter_complex", fc, "-map", last,
    ]
    if has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-profile:v", "high",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", out.name,
    ]
    # run inside the output folder so the subtitles filter gets a plain relative filename
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=out.parent)
    if proc.returncode != 0:
        raise MediaError("ffmpeg render failed:\n" + "\n".join(proc.stderr.strip().splitlines()[-12:]))
    return used
