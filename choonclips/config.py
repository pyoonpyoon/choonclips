"""Settings, read from environment variables (or a .env file in the working directory)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("CHOONCLIPS_DATA_DIR", "data")).resolve())
    host: str = field(default_factory=lambda: _env("CHOONCLIPS_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("CHOONCLIPS_PORT", 8000))
    # The URL your browser uses for this app. OAuth redirect URIs are built from it:
    #   {base_url}/oauth/{platform}/callback
    base_url: str = field(default_factory=lambda: _env("CHOONCLIPS_BASE_URL", "http://localhost:8000").rstrip("/"))

    # Clip shape
    min_clip_seconds: int = field(default_factory=lambda: _int("CLIP_MIN_SECONDS", 15))
    max_clip_seconds: int = field(default_factory=lambda: _int("CLIP_MAX_SECONDS", 20))
    target_clip_seconds: int = field(default_factory=lambda: _int("CLIP_TARGET_SECONDS", 18))
    # 0 = as many good clips as the video supports
    max_clips: int = field(default_factory=lambda: _int("CLIP_MAX_COUNT", 0))
    burn_captions: bool = field(default_factory=lambda: _env("BURN_CAPTIONS", "1") != "0")

    # Speech to text
    whisper_model: str = field(default_factory=lambda: _env("WHISPER_MODEL", "small"))
    whisper_device: str = field(default_factory=lambda: _env("WHISPER_DEVICE", "auto"))

    # Claude (optional). Without a key the local heuristic scorer is used.
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-opus-5-5"))

    # Platform apps (you register these yourself, see README)
    youtube_client_id: str = field(default_factory=lambda: _env("YOUTUBE_CLIENT_ID"))
    youtube_client_secret: str = field(default_factory=lambda: _env("YOUTUBE_CLIENT_SECRET"))
    tiktok_client_key: str = field(default_factory=lambda: _env("TIKTOK_CLIENT_KEY"))
    tiktok_client_secret: str = field(default_factory=lambda: _env("TIKTOK_CLIENT_SECRET"))
    instagram_app_id: str = field(default_factory=lambda: _env("INSTAGRAM_APP_ID"))
    instagram_app_secret: str = field(default_factory=lambda: _env("INSTAGRAM_APP_SECRET"))
    x_client_id: str = field(default_factory=lambda: _env("X_CLIENT_ID"))
    x_client_secret: str = field(default_factory=lambda: _env("X_CLIENT_SECRET"))

    # TikTok: unaudited apps may only post privately. Set to PUBLIC_TO_EVERYONE once audited.
    tiktok_privacy: str = field(default_factory=lambda: _env("TIKTOK_PRIVACY", "SELF_ONLY"))
    # YouTube: public | unlisted | private
    youtube_privacy: str = field(default_factory=lambda: _env("YOUTUBE_PRIVACY", "public"))

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "choonclips.db"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.uploads_dir, self.work_dir, self.clips_dir):
            d.mkdir(parents=True, exist_ok=True)

    def redirect_uri(self, platform: str) -> str:
        return f"{self.base_url}/oauth/{platform}/callback"


settings = Settings()
