"""Getting a source video onto disk: uploads and URL imports (YouTube etc. via yt-dlp)."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..config import settings


def download_url(url: str, video_id: str, on_progress: Callable[[float, str], None] | None = None) -> tuple[Path, str]:
    """Download `url` with yt-dlp. Returns (path, title).

    Only download videos you own or have the rights to repost.
    """
    try:
        import yt_dlp  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("URL import needs yt-dlp: pip install 'clipforge[url]'") from e

    out_dir = settings.uploads_dir / video_id
    out_dir.mkdir(parents=True, exist_ok=True)

    def hook(d: dict) -> None:
        if on_progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                on_progress(done / total, "Downloading")

    opts = {
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    if not path.exists():
        mp4 = path.with_suffix(".mp4")
        if mp4.exists():
            path = mp4
        else:
            candidates = sorted(out_dir.glob("source.*"))
            if not candidates:
                raise RuntimeError("yt-dlp finished but no file was written")
            path = candidates[0]
    return path, info.get("title") or url
