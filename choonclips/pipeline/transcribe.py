"""Speech to text with word-level timestamps (faster-whisper, runs locally)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from ..config import settings


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Segment:
    text: str
    start: float
    end: float
    words: list[Word]


_model = None


def _load_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # type: ignore

        device = settings.whisper_device
        compute = "int8"
        if device == "auto":
            try:
                import ctranslate2  # type: ignore

                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"
        if device == "cuda":
            compute = "float16"
        _model = WhisperModel(settings.whisper_model, device=device, compute_type=compute)
    return _model


def available() -> bool:
    try:
        import faster_whisper  # type: ignore  # noqa: F401

        return True
    except ImportError:
        return False


def transcribe(audio: Path, duration: float, on_progress: Callable[[float], None] | None = None) -> list[Segment]:
    model = _load_model()
    seg_iter, _info = model.transcribe(str(audio), word_timestamps=True, vad_filter=True, beam_size=5)
    out: list[Segment] = []
    for s in seg_iter:
        words = [Word(w.word.strip(), float(w.start), float(w.end)) for w in (s.words or []) if w.word.strip()]
        out.append(Segment(s.text.strip(), float(s.start), float(s.end), words))
        if on_progress and duration:
            on_progress(min(1.0, s.end / duration))
    return out


def save(segments: list[Segment], path: Path) -> None:
    path.write_text(json.dumps([asdict(s) for s in segments]))


def load(path: Path) -> list[Segment]:
    raw = json.loads(path.read_text())
    return [Segment(s["text"], s["start"], s["end"], [Word(**w) for w in s["words"]]) for s in raw]
