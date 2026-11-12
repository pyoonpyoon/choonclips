"""Find clip windows and score them with local signals (no network needed).

A clip should:
  * start at the beginning of a thought and end at the end of one,
  * open with a hook in the first ~3 seconds,
  * be dense (talking, not dead air) and energetic,
  * fit the target length for short-form feeds.

This module turns a transcript + loudness envelope + cut list into ranked, non-overlapping windows.
The Claude scorer (scoring.py) can replace the ranking; this is the offline fallback and also
supplies the "signals" both scorers use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .transcribe import Segment, Word

HOP = 0.5  # seconds per loudness frame (see media.audio_rms)

HOOK_WORDS = {
    "secret", "secrets", "never", "nobody", "everyone", "mistake", "mistakes", "truth", "actually",
    "crazy", "insane", "wild", "shocking", "biggest", "worst", "best", "stop", "why", "how", "what",
    "nobody's", "don't", "won't", "can't", "hate", "love", "million", "billion", "money", "rich",
    "broke", "fired", "quit", "died", "dead", "illegal", "banned", "lie", "lied", "lies", "wrong",
    "honestly", "imagine", "listen", "watch", "warning", "hack", "trick", "rule", "rules", "nobody",
    "first", "last", "only", "real", "really", "literally", "unbelievable", "changed", "regret",
}
EMOTION_WORDS = {
    "amazing", "incredible", "terrible", "awful", "hilarious", "funny", "scared", "afraid", "angry",
    "furious", "love", "hate", "obsessed", "cried", "crying", "laughing", "shocked", "insane", "crazy",
    "unbelievable", "disgusting", "beautiful", "perfect", "nightmare", "dream", "wow", "oh", "omg",
}
FILLER_START = {"um", "uh", "so", "and", "but", "like", "yeah", "okay", "ok", "right", "anyway"}

_WORD = re.compile(r"[a-z0-9']+")


@dataclass
class Unit:
    """A sentence-ish run of words."""
    start: float
    end: float
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words).strip()


@dataclass
class Candidate:
    start: float
    end: float
    score: float = 0.0
    title: str = ""
    hook: str = ""
    reason: str = ""
    transcript: str = ""
    captions: dict[str, str] = field(default_factory=dict)
    hashtags: list[str] = field(default_factory=list)
    signals: dict[str, float] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start


def tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def build_units(segments: list[Segment], max_unit: float = 12.0, gap: float = 0.8) -> list[Unit]:
    words = [w for s in segments for w in s.words]
    if not words:
        # Segments without word timings: use segments as units
        return [Unit(s.start, s.end, [Word(s.text, s.start, s.end)]) for s in segments if s.text]
    units: list[Unit] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        if cur and (w.start - cur[-1].end > gap):
            units.append(Unit(cur[0].start, cur[-1].end, cur))
            cur = []
        cur.append(w)
        ends_sentence = bool(re.search(r"[.?!…]['\"]?$", w.text))
        too_long = cur[-1].end - cur[0].start >= max_unit and w.text.endswith(",")
        if ends_sentence or too_long or cur[-1].end - cur[0].start >= max_unit * 1.5:
            units.append(Unit(cur[0].start, cur[-1].end, cur))
            cur = []
    if cur:
        units.append(Unit(cur[0].start, cur[-1].end, cur))
    return units


def windows_from_units(units: list[Unit], min_s: float, max_s: float) -> list[tuple[int, int]]:
    """All (i, j) unit ranges whose span is within [min_s, max_s]."""
    out = []
    for i in range(len(units)):
        for j in range(i, len(units)):
            span = units[j].end - units[i].start
            if span > max_s:
                break
            if span >= min_s:
                out.append((i, j))
    return out


def _zscore(a: np.ndarray) -> np.ndarray:
    if a.size == 0:
        return a
    sd = a.std()
    return (a - a.mean()) / (sd if sd > 1e-9 else 1.0)


class SignalScorer:
    def __init__(self, energy: np.ndarray, cuts: list[float], duration: float, target: float):
        self.energy = energy
        self.ez = _zscore(np.log(energy + 1e-6)) if energy.size else energy
        self.cuts = np.array(sorted(cuts)) if cuts else np.zeros(0)
        self.duration = duration
        self.target = target

    def _energy(self, start: float, end: float) -> tuple[float, float]:
        if self.ez.size == 0:
            return 0.0, 0.0
        a, b = int(start / HOP), max(int(start / HOP) + 1, int(end / HOP))
        seg = self.ez[a:b]
        if seg.size == 0:
            return 0.0, 0.0
        return float(seg.mean()), float(np.percentile(seg, 90))

    def _cut_rate(self, start: float, end: float) -> float:
        if self.cuts.size == 0:
            return 0.0
        n = int(((self.cuts >= start) & (self.cuts < end)).sum())
        return n / max(1.0, end - start) * 10  # cuts per 10 s

    def score(self, start: float, end: float, text: str, words: list[Word]) -> tuple[float, dict[str, float]]:
        dur = end - start
        toks = tokens(text)
        first = tokens(" ".join(w.text for w in words if w.start < start + 3.5)) or toks[:8]

        hook = 0.0
        if text.strip().split(".")[0].strip().endswith("?") or "?" in " ".join(w.text for w in words[:15]):
            hook += 1.0
        hook += 0.6 * sum(1 for t in first if t in HOOK_WORDS)
        hook += 0.5 * sum(1 for t in first if t.isdigit())
        hook += 0.4 * (1 if first and first[0] in {"you", "your", "i", "my", "this", "here's", "nobody", "stop"} else 0)
        hook -= 0.8 * (1 if first and first[0] in FILLER_START else 0)
        hook = float(np.clip(hook / 2.5, 0, 1))

        emotion = sum(1 for t in toks if t in EMOTION_WORDS) + text.count("!")
        emotion = float(np.clip(emotion / max(1, dur / 10) / 2.0, 0, 1))

        wps = len(toks) / max(dur, 1e-6)
        # 2.3-3.5 words/sec is engaging talking pace
        pace = float(np.clip(1 - abs(wps - 2.9) / 2.0, 0, 1)) if toks else 0.0

        speech_time = sum(max(0.0, w.end - w.start) for w in words)
        density = float(np.clip(speech_time / max(dur, 1e-6) / 0.8, 0, 1)) if words else 0.0

        e_mean, e_peak = self._energy(start, end)
        energy = float(np.clip((e_mean + 1) / 2.5, 0, 1)) * 0.6 + float(np.clip((e_peak + 1) / 3, 0, 1)) * 0.4

        visual = float(np.clip(self._cut_rate(start, end) / 3.0, 0, 1))
        length = float(np.exp(-((dur - self.target) / max(6.0, self.target * 0.5)) ** 2))
        clean_end = 1.0 if re.search(r"[.?!]['\"]?$", text.strip()) else 0.4

        signals = {
            "hook": hook, "emotion": emotion, "pace": pace, "density": density,
            "energy": energy, "visual": visual, "length": length, "clean_end": clean_end,
        }
        weights = {
            "hook": 0.28, "emotion": 0.12, "pace": 0.10, "density": 0.10,
            "energy": 0.16, "visual": 0.06, "length": 0.10, "clean_end": 0.08,
        }
        total = sum(signals[k] * w for k, w in weights.items()) * 100
        return round(total, 1), {k: round(v, 3) for k, v in signals.items()}


def overlap_frac(a: Candidate, b: Candidate) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    return inter / max(1e-6, min(a.duration, b.duration))


def select(cands: list[Candidate], max_clips: int, max_overlap: float = 0.08, min_score: float = 0.0) -> list[Candidate]:
    picked: list[Candidate] = []
    for c in sorted(cands, key=lambda c: c.score, reverse=True):
        if c.score < min_score and len(picked) >= 3:
            break
        if all(overlap_frac(c, p) <= max_overlap for p in picked):
            picked.append(c)
            if max_clips and len(picked) >= max_clips:
                break
    return picked


def heuristic_candidates(
    segments: list[Segment],
    energy: np.ndarray,
    cuts: list[float],
    duration: float,
    min_s: float,
    max_s: float,
    target: float,
) -> list[Candidate]:
    scorer = SignalScorer(energy, cuts, duration, target)
    units = build_units(segments)
    cands: list[Candidate] = []
    if units:
        for i, j in windows_from_units(units, min_s, max_s):
            ws = [w for u in units[i : j + 1] for w in u.words]
            text = " ".join(u.text for u in units[i : j + 1])
            start = max(0.0, units[i].start - 0.15)
            end = min(duration or units[j].end + 0.3, units[j].end + 0.35)
            s, sig = scorer.score(start, end, text, ws)
            cands.append(Candidate(start, end, s, transcript=text, signals=sig))
    else:
        # No speech: slide fixed windows and rank on loudness + cuts
        step = 3.0
        t = 0.0
        while t + target <= duration:
            s, sig = scorer.score(t, t + target, "", [])
            cands.append(Candidate(t, t + target, s, signals=sig))
            t += step
    return cands


def text_between(segments: list[Segment], start: float, end: float) -> tuple[str, list[Word]]:
    ws = [w for s in segments for w in s.words if w.start >= start - 0.05 and w.end <= end + 0.05]
    return " ".join(w.text for w in ws).strip(), ws


def snap_to_words(segments: list[Segment], start: float, end: float) -> tuple[float, float]:
    """Nudge LLM-proposed boundaries so we never cut a word in half."""
    words = [w for s in segments for w in s.words]
    if not words:
        return start, end
    starts = [w.start for w in words]
    ends = [w.end for w in words]
    s = min(starts, key=lambda x: abs(x - start))
    e = min(ends, key=lambda x: abs(x - end))
    if e <= s:
        e = s + (end - start)
    return max(0.0, s - 0.15), e + 0.35


def default_hook(text: str, max_words: int = 9) -> str:
    """First sentence if it is short enough to read in a glance, else the first few words."""
    first = re.split(r"(?<=[.?!])\s+", text.strip(), maxsplit=1)[0]
    if 2 <= len(first.split()) <= max_words:
        return first.rstrip(".")
    return default_title(text, 7)


def default_title(text: str, n: int = 8) -> str:
    words = text.split()
    t = " ".join(words[:n]).strip(" ,.;:")
    return (t + "…") if len(words) > n else t
