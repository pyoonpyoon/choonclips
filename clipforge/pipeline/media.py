"""ffmpeg / ffprobe helpers."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np


class MediaError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise MediaError(f"{tool} not found on PATH. Install FFmpeg (https://ffmpeg.org/download.html).")


def run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-15:]
        raise MediaError(f"{Path(cmd[0]).name} failed:\n" + "\n".join(tail))
    return proc


def probe(path: Path) -> dict:
    out = run([
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
    ]).stdout
    info = json.loads(out)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError("No video stream found in file.")
    w, h = int(video.get("width", 0)), int(video.get("height", 0))
    # Honour rotation metadata (phones)
    rot = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if abs(rot) in (90, 270):
        w, h = h, w
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    fps = 30.0
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    except (ValueError, ZeroDivisionError):
        pass
    return {"duration": duration, "width": w, "height": h, "fps": fps, "has_audio": audio is not None}


def extract_audio_wav(src: Path, dst: Path, sr: int = 16000) -> Path:
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(dst)])
    return dst


def audio_rms(src: Path, hop: float = 0.5, sr: int = 8000) -> np.ndarray:
    """Loudness envelope (RMS per `hop` seconds) of the whole file."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return np.zeros(0)
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    n = int(sr * hop)
    usable = len(pcm) - len(pcm) % n
    if usable <= 0:
        return np.zeros(0)
    frames = pcm[:usable].reshape(-1, n)
    return np.sqrt((frames**2).mean(axis=1) + 1e-12)


_PTS = re.compile(r"pts_time:([0-9.]+)")


def scene_changes(src: Path, threshold: float = 0.35) -> list[float]:
    """Timestamps of hard cuts. Runs on a tiny downscale, so it is fast."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(src), "-an",
         "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return [float(m.group(1)) for m in _PTS.finditer(proc.stderr or "")]


def thumbnail(src: Path, t: float, dst: Path) -> Path:
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1", "-vf", "scale=360:-2", str(dst)])
    return dst
