"""Burned-in captions in the short-form style: 2-3 big words at a time, current word highlighted."""
from __future__ import annotations

from .transcribe import Word

W, H = 1080, 1920


def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words: list[Word], clip_start: float, clip_end: float, hook: str = "", group: int = 3) -> str:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,80,80,560,1
Style: Hook,Arial Black,60,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = []
    if hook:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(min(3.0, clip_end - clip_start))},Hook,,0,0,0,,{_esc(hook.upper())}")

    ws = [w for w in words if w.end > clip_start and w.start < clip_end and w.text.strip()]
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in ws:
        cur.append(w)
        if len(cur) >= group or w.text.rstrip()[-1:] in ".?!,":
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    for gi, g in enumerate(groups):
        g_end = groups[gi + 1][0].start if gi + 1 < len(groups) else g[-1].end + 0.3
        for wi, w in enumerate(g):
            a = w.start - clip_start
            b = (g[wi + 1].start if wi + 1 < len(g) else g_end) - clip_start
            if b <= a:
                b = a + 0.08
            parts = []
            for k, x in enumerate(g):
                txt = _esc(x.text.strip().upper())
                parts.append("{\\c&H00E5FF&\\fscx108\\fscy108}" + txt + "{\\c&HFFFFFF&\\fscx100\\fscy100}" if k == wi else txt)
            lines.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(lines) + "\n"
