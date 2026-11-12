"""Decide where to crop a landscape video to get a 9:16 frame that keeps the speaker in shot.

We sample frames, find faces, and pick one horizontal crop position per *shot* (between hard cuts),
which is what a human editor does: the frame stays still within a shot and re-centres on a cut.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def available() -> bool:
    try:
        import cv2  # type: ignore

        # OpenCV 5 removed the Haar cascade face detector we use
        return hasattr(cv2, "CascadeClassifier")
    except ImportError:
        return False


def face_track(src: Path, start: float, end: float, step: float = 0.5) -> list[tuple[float, float | None]]:
    """[(t_rel, face_center_x_fraction_or_None)] sampled every `step` seconds."""
    import cv2  # type: ignore

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(src))
    out: list[tuple[float, float | None]] = []
    try:
        t = start
        while t < end:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            h, w = frame.shape[:2]
            scale = 480 / max(w, 1)
            small = cv2.resize(frame, (480, max(1, int(h * scale))))
            gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
            cx = None
            if len(faces):
                # Biggest face = most likely the speaker
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                cx = (x + fw / 2) / small.shape[1]
            out.append((t - start, cx))
            t += step
    finally:
        cap.release()
    return out


def plan_crop(
    samples: list[tuple[float, float | None]],
    cuts_rel: list[float],
    duration: float,
) -> list[tuple[float, float]] | None:
    """Return [(shot_start_rel, center_x_fraction)] or None if no faces were found at all."""
    if not samples or all(c is None for _, c in samples):
        return None
    bounds = [0.0] + [c for c in sorted(cuts_rel) if 0.3 < c < duration - 0.3] + [duration + 1]
    plan: list[tuple[float, float]] = []
    last = 0.5
    for a, b in zip(bounds, bounds[1:]):
        xs = [c for t, c in samples if a <= t < b and c is not None]
        if xs:
            last = float(np.median(xs))
        plan.append((a, last))
    # merge shots with nearly the same framing to avoid micro-jumps
    merged: list[tuple[float, float]] = []
    for t, x in plan:
        if merged and abs(merged[-1][1] - x) < 0.04:
            continue
        merged.append((t, x))
    return merged


def crop_x_expr(plan: list[tuple[float, float]], src_w: int, crop_w: int) -> str:
    """ffmpeg expression for the crop filter's x, piecewise constant over time."""
    def px(frac: float) -> int:
        return int(np.clip(frac * src_w - crop_w / 2, 0, src_w - crop_w))

    expr = str(px(plan[-1][1]))
    for (t, x), (t_next, _) in reversed(list(zip(plan, plan[1:]))):
        expr = f"if(lt(t\\,{t_next:.3f})\\,{px(x)}\\,{expr})"
    return expr
