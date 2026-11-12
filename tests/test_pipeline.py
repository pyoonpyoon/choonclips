import numpy as np

from choonclips.pipeline import candidates as C
from choonclips.pipeline import scoring
from choonclips.pipeline.captions import build_ass
from choonclips.pipeline.reframe import crop_x_expr, plan_crop
from choonclips.pipeline.transcribe import Segment, Word


def make_segments(sentences, t=0.0, gap=0.4):
    segs = []
    for s in sentences:
        words = []
        for w in s.split():
            words.append(Word(w, t, t + 0.3))
            t += 0.38
        segs.append(Segment(s, words[0].start, words[-1].end, words))
        t += gap
    return segs


FILLER = "so yeah we were just kind of talking about stuff and things."
HOOK = "Nobody tells you the secret to making a million dollars? Here's why."


def test_units_split_on_sentences():
    segs = make_segments(["Hello there.", "How are you?"])
    units = C.build_units(segs)
    assert [u.text for u in units] == ["Hello there.", "How are you?"]


def test_windows_respect_bounds():
    segs = make_segments([FILLER] * 20)
    units = C.build_units(segs)
    for i, j in C.windows_from_units(units, 15, 30):
        span = units[j].end - units[i].start
        assert 15 <= span <= 30


def test_select_no_overlap():
    cands = [C.Candidate(0, 20, 90), C.Candidate(5, 25, 80), C.Candidate(30, 50, 70)]
    picked = C.select(cands, 0)
    assert [(c.start, c.end) for c in picked] == [(0, 20), (30, 50)]


def test_select_respects_max():
    cands = [C.Candidate(i * 30, i * 30 + 20, 100 - i) for i in range(10)]
    assert len(C.select(cands, 3)) == 3


def test_hook_window_outranks_filler():
    sents = [FILLER] * 8 + [HOOK, "It is about time, not money!", "You have to start today."] + [FILLER] * 8
    segs = make_segments(sents)
    dur = segs[-1].end + 1
    picked, strategy = scoring.pick_clips(segs, np.zeros(0), [], dur, "t", 10, 30, 15, 1, use_ai=False)
    assert strategy == "heuristic"
    assert "Nobody" in picked[0].transcript.split(".")[0] or picked[0].transcript.startswith("Nobody")
    assert picked[0].captions["tiktok"]
    assert picked[0].hook


def test_no_speech_fallback():
    energy = np.ones(200)
    energy[100:140] = 5
    picked, _ = scoring.pick_clips([], energy, [], 100, "t", 10, 30, 15, 2, use_ai=False)
    assert len(picked) == 2


def test_claude_path_is_snapped_and_blended(monkeypatch):
    segs = make_segments([FILLER] * 6 + [HOOK] + [FILLER] * 6)
    monkeypatch.setattr(scoring, "claude_available", lambda: True)
    monkeypatch.setattr(scoring, "claude_pick", lambda *a, **k: [{
        "start": segs[6].start + 0.05, "end": segs[9].end - 0.02, "virality_score": 90, "title": "T", "hook": "H",
        "reason": "R", "hashtags": ["#money"],
        "captions": {"tiktok": "a", "instagram": "b", "youtube_title": "c", "youtube_description": "d", "x": "e"},
    }])
    picked, strategy = scoring.pick_clips(segs, np.zeros(0), [], segs[-1].end, "t", 10, 40, 20, 0)
    assert strategy == "claude"
    c = picked[0]
    assert abs(c.start - (segs[6].start - 0.15)) < 1e-6
    assert c.hashtags == ["money"]
    assert c.title == "T" and c.captions["x"] == "e"


def test_claude_failure_falls_back(monkeypatch):
    segs = make_segments([FILLER] * 10)
    monkeypatch.setattr(scoring, "claude_available", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(scoring, "claude_pick", boom)
    picked, strategy = scoring.pick_clips(segs, np.zeros(0), [], segs[-1].end, "t", 10, 30, 15, 0)
    assert strategy == "heuristic" and picked


def test_crop_expression_piecewise():
    plan = plan_crop([(0, 0.2), (1, 0.2), (5, 0.8), (6, 0.8)], [4.0], 8.0)
    assert plan == [(0.0, 0.2), (4.0, 0.8)]
    expr = crop_x_expr(plan, 1920, 606)
    assert expr.startswith("if(lt(t\\,4.000)\\,81\\,") and expr.endswith("1233)")


def test_plan_none_without_faces():
    assert plan_crop([(0, None), (1, None)], [], 2) is None


def test_ass_highlights_each_word():
    words = [Word("hello", 10.0, 10.3), Word("big", 10.4, 10.6), Word("world.", 10.7, 11.0)]
    ass = build_ass(words, 10.0, 12.0, hook="Hook {x}")
    dialogue = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    assert len(dialogue) == 4  # hook + one per word
    assert "HOOK (X)" in dialogue[0]
    assert "0:00:00.00" in dialogue[1]
