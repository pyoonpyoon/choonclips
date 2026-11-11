"""Pick and package clips.

Two strategies:
  * Claude (if ANTHROPIC_API_KEY is set): reads the timestamped transcript, picks the moments most
    likely to work as standalone shorts, and writes a title, hook, per-platform captions and hashtags.
    Its picks are snapped to word boundaries and blended with the local signal score.
  * Heuristic (always available): see candidates.py.
"""
from __future__ import annotations

import json
import logging

import numpy as np

from ..config import settings
from .candidates import (
    Candidate,
    SignalScorer,
    build_units,
    default_hook,
    default_title,
    heuristic_candidates,
    select,
    snap_to_words,
    text_between,
    tokens,
)
from .transcribe import Segment

log = logging.getLogger(__name__)

PLATFORM_KEYS = ["tiktok", "instagram", "youtube_title", "youtube_description", "x"]

CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "description": "seconds"},
                    "end": {"type": "number", "description": "seconds"},
                    "virality_score": {"type": "integer", "description": "0-100"},
                    "title": {"type": "string"},
                    "hook": {"type": "string", "description": "on-screen hook text, max ~8 words"},
                    "reason": {"type": "string"},
                    "captions": {
                        "type": "object",
                        "properties": {k: {"type": "string"} for k in PLATFORM_KEYS},
                        "required": PLATFORM_KEYS,
                        "additionalProperties": False,
                    },
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["start", "end", "virality_score", "title", "hook", "reason", "captions", "hashtags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}

SYSTEM = """You are a short-form video editor who finds the moments in long videos that will perform \
as standalone clips on TikTok, Instagram Reels, YouTube Shorts and X.

What makes a moment work:
- It opens with a hook in the first 1-3 seconds: a bold claim, a question, a surprising number, \
conflict, a punchline setup, or an emotional beat. Never start on filler ("so", "um", "anyway") \
or mid-thought.
- It is self-contained: a viewer with no context understands it and it pays off (answer, punchline, \
reveal, strong opinion, useful tip, story beat) before it ends.
- It ends cleanly at the end of a sentence, ideally right after the payoff.
- High information or emotion density; no long pauses or tangents.

Rules:
- Use only timestamps that appear in the transcript. start = the start time of the first line you \
include; end = the end time of the last line you include.
- Clips must not overlap.
- Prefer quality over quantity, but when a video is long and full of strong moments, return many.
- virality_score is your honest 0-100 estimate of how well the clip would do relative to the other \
clips from this same video; reserve 80+ for genuinely strong moments.
- Captions: write natural, platform-native copy. TikTok/Instagram: 1-2 short lines plus a soft call \
to action. youtube_title: under 70 characters, curiosity-driven, no clickbait lies. \
youtube_description: 1-2 sentences. x: under 240 characters, no hashtags in the body.
- hashtags: 3-6 relevant tags without the # sign, mixing broad and niche.
- Never invent things the speaker did not say."""


def _transcript_lines(segments: list[Segment]) -> str:
    units = build_units(segments)
    return "\n".join(f"[{u.start:.2f}-{u.end:.2f}] {u.text}" for u in units)


def claude_available() -> bool:
    if not settings.anthropic_api_key:
        return False
    try:
        import anthropic  # type: ignore  # noqa: F401

        return True
    except ImportError:
        return False


def claude_pick(
    segments: list[Segment],
    duration: float,
    title: str,
    min_s: float,
    max_s: float,
    target: float,
    max_clips: int,
) -> list[dict]:
    import anthropic  # type: ignore

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    count = (
        f"Return up to {max_clips} clips."
        if max_clips
        else "Return as many strong clips as the video genuinely supports (roughly one per strong moment; "
        "a 10-minute video often has 3-8, a 1-hour podcast 15-40)."
    )
    user = (
        f"Video title: {title}\nDuration: {duration:.1f}s\n"
        f"Clip length: {min_s}-{max_s}s, ideally around {target}s.\n{count}\n\n"
        f"Transcript (each line is [start-end] text, in seconds):\n{_transcript_lines(segments)}"
    )
    with client.beta.messages.stream(
        model=settings.claude_model,
        max_tokens=64000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": CLIP_SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude declined to analyse this transcript")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off (max_tokens)")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    return json.loads(text).get("clips", [])


def _fallback_package(c: Candidate, video_title: str) -> None:
    text = c.transcript or video_title
    c.title = c.title or default_title(text) or video_title
    c.hook = c.hook or default_hook(text)
    words = [t for t in tokens(text) if len(t) > 4]
    common = sorted(set(words), key=lambda w: -words.count(w))[:3]
    c.hashtags = c.hashtags or (common + ["shorts", "fyp"])
    tags = " ".join("#" + h for h in c.hashtags)
    c.captions = c.captions or {
        "tiktok": f"{c.hook}\n{tags}",
        "instagram": f"{c.hook}\n\nFollow for more.\n{tags}",
        "youtube_title": (c.title[:90] + " #shorts"),
        "youtube_description": f"{c.transcript[:200]}\n\nFrom: {video_title}\n{tags}",
        "x": c.hook,
    }
    c.reason = c.reason or "Ranked by local signals: " + ", ".join(
        f"{k} {v:.2f}" for k, v in sorted(c.signals.items(), key=lambda kv: -kv[1])[:3]
    )


def pick_clips(
    segments: list[Segment],
    energy: np.ndarray,
    cuts: list[float],
    duration: float,
    video_title: str,
    min_s: float | None = None,
    max_s: float | None = None,
    target: float | None = None,
    max_clips: int | None = None,
    use_ai: bool = True,
) -> tuple[list[Candidate], str]:
    """Returns (clips ranked best-first, strategy name)."""
    min_s = float(min_s or settings.min_clip_seconds)
    max_s = float(max_s or settings.max_clip_seconds)
    target = float(target or settings.target_clip_seconds)
    max_clips = settings.max_clips if max_clips is None else max_clips
    if duration and duration < min_s:
        min_s = max(3.0, duration * 0.5)
        max_s = duration
        target = duration
    scorer = SignalScorer(energy, cuts, duration, target)

    has_words = any(s.words for s in segments)
    if use_ai and has_words and claude_available():
        try:
            raw = claude_pick(segments, duration, video_title, min_s, max_s, target, max_clips)
            cands: list[Candidate] = []
            for r in raw:
                s, e = snap_to_words(segments, float(r["start"]), float(r["end"]))
                e = min(e, duration) if duration else e
                if e - s < min_s * 0.7 or e - s > max_s * 1.3:
                    continue
                text, ws = text_between(segments, s, e)
                sig_score, sig = scorer.score(s, e, text, ws)
                llm = float(np.clip(r.get("virality_score", 50), 0, 100))
                cands.append(Candidate(
                    s, e, round(0.75 * llm + 0.25 * sig_score, 1),
                    title=r.get("title", ""), hook=r.get("hook", ""), reason=r.get("reason", ""),
                    transcript=text, captions=r.get("captions", {}),
                    hashtags=[h.lstrip("#").replace(" ", "") for h in r.get("hashtags", [])],
                    signals={**sig, "llm": llm / 100, "signal_score": sig_score / 100},
                ))
            picked = select(cands, max_clips)
            if picked:
                for c in picked:
                    _fallback_package(c, video_title)  # fills any blanks only
                return picked, "claude"
            log.warning("Claude returned no usable clips; using heuristic")
        except Exception as e:  # network, auth, parse... never block the pipeline
            log.warning("Claude scoring failed (%s); using heuristic", e)

    cands = heuristic_candidates(segments, energy, cuts, duration, min_s, max_s, target)
    if not max_clips:
        # "as many as possible": every non-overlapping window that scores in the top half
        threshold = float(np.percentile([c.score for c in cands], 50)) if cands else 0
        picked = select(cands, 0, min_score=threshold)
    else:
        picked = select(cands, max_clips)
    for c in picked:
        _fallback_package(c, video_title)
    return picked, "heuristic"
