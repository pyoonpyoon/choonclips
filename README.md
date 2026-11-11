# ClipForge

**A local clipping studio for short-form video.** Drop in a long video (podcast, stream, interview, vlog) or paste a YouTube link. ClipForge finds the moments most likely to work as shorts, cuts them to 15–45 s vertical clips with speaker-tracked framing and animated captions, writes a title, hook and caption for each platform, and posts them to **TikTok, Instagram Reels, YouTube Shorts and X**, either right away or on a schedule.

Everything runs on your machine. Your videos never leave it except when you post them.

```
 long video ─▶ transcribe ─▶ find moments ─▶ score ─▶ render 9:16 ─▶ package ─▶ post / schedule
  (upload     (Whisper,     (sentence-      (Claude   (face-tracked   (per-platform   (TikTok, IG,
   or URL)     word-level)   aligned         or local  crop, burned    captions +      YT Shorts, X)
                             windows)        signals)  captions)       hashtags)
```

## Features

- **Upload or import.** Drag in MP4/MOV/MKV/WEBM, or paste a YouTube (or other) URL via yt-dlp.
- **As many clips as the video supports.** Set *Max clips* to 0 and ClipForge returns every strong, non-overlapping moment. A 1-hour podcast typically gives 15–40.
- **Smart picking.** With an Anthropic API key, Claude reads the timestamped transcript and picks self-contained moments with a hook in the first seconds and a payoff before the end. It also writes the title, on-screen hook and captions. Without a key, a local scorer ranks sentence-aligned windows on hook words, questions and numbers, emotion, talking pace, speech density, loudness, cut rate and length.
- **Clean cuts.** Clips start and end on word and sentence boundaries, never mid-word.
- **Vertical framing.** Faces are tracked, and the 9:16 crop re-centres once per shot, like an editor would. You can also choose a centre crop, fill, or the full frame over a blurred background.
- **Captions.** Big 2–3-word captions with the spoken word highlighted, plus a hook banner for the first 3 seconds.
- **One file for every platform.** 1080×1920 H.264/AAC, 30 fps, loudness-normalised to −14 LUFS.
- **Edit before posting.** Change the title, hook, per-platform captions and hashtags, or trim the start and end (the clip re-renders).
- **Auto-posting.** Connect each account once with OAuth. Post now, or schedule a batch with spacing (e.g. one clip every 4 hours). Failed posts retry and are listed in the queue.
- **Manual fallback.** *Download* gives a zip with the clip, a cover image and ready-to-paste captions for every platform.

## Quick start

Requirements: Python 3.10+ and [FFmpeg](https://ffmpeg.org/download.html) on your PATH (with libass, which most builds include, for captions).

```bash
git clone <this repo> clipforge && cd clipforge
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[all]"
cp .env.example .env                                     # optional: add keys
clipforge                                                # → http://localhost:8000
```

`[all]` installs every optional part. You can pick just the ones you want instead:

| Extra | What it enables | Without it |
|---|---|---|
| `transcribe` (faster-whisper) | Transcript, captions, transcript-based picking | Clips picked from loudness and cuts only, no captions |
| `reframe` (OpenCV 4) | Speaker-tracked 9:16 crop | Blurred-background fit |
| `ai` (anthropic) + `ANTHROPIC_API_KEY` | Claude picks clips and writes copy | Local heuristic scorer and template captions |
| `url` (yt-dlp) | Import from YouTube and other URLs | Upload files only |

The first transcription downloads the Whisper model you set (`WHISPER_MODEL`, default `small`). An NVIDIA GPU is used automatically if available.

## Connecting accounts

Each platform needs you to register your own developer app, which is free (X's media posting needs a paid API tier). In every app, register this redirect URI (the Accounts page shows it for each platform):

```
http://localhost:8000/oauth/<platform>/callback        # platform = youtube | tiktok | instagram | x
```

Put the keys in `.env`, restart, open **Accounts** and click **Connect**.

| Platform | Where | What to enable | Things to know |
|---|---|---|---|
| **YouTube Shorts** | Google Cloud Console → enable *YouTube Data API v3* → OAuth client ID (*Web application*) | Scope `youtube.upload` | Until Google audits your project, API uploads are forced to **private**. Add yourself as a test user on the OAuth consent screen. Vertical videos ≤ 3 min become Shorts automatically. |
| **TikTok** | developers.tiktok.com → create app → add *Login Kit* and *Content Posting API* (Direct Post) | Scopes `user.info.basic`, `video.publish`, `video.upload` | Until TikTok audits the app, posts are limited to **private (SELF_ONLY)**. After approval, set `TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE`. TikTok may require an `https` redirect URI; if so, use a tunnel (below) and set `CLIPFORGE_BASE_URL` to it. |
| **Instagram Reels** | developers.facebook.com → create app → *Instagram API with Instagram Login* | `instagram_business_basic`, `instagram_business_content_publish` | Needs an Instagram **Business or Creator** account. Clips upload straight from your machine by default. If your app can't use resumable upload, set `INSTAGRAM_UPLOAD_MODE=url` and expose ClipForge with a tunnel (`CLIPFORGE_PUBLIC_URL`). |
| **X** | developer.x.com → project → app → *User authentication settings*: OAuth 2.0, type *Web App* | `tweet.read tweet.write users.read media.write offline.access` | Video posting needs a paid API tier. |

**Tunnels.** If a platform insists on a public `https` redirect URI, run e.g. `cloudflared tunnel --url http://localhost:8000` and set `CLIPFORGE_BASE_URL` (and `CLIPFORGE_PUBLIC_URL`) to the URL it prints.

**Scheduling.** Scheduled posts go out from this app, so ClipForge has to be running at the scheduled time (leave it running, or run it as a service).

## Configuration

All settings live in `.env`. See [`.env.example`](.env.example) for the full list: clip length bounds, clip count, caption burn-in, Whisper model and device, Claude model, privacy defaults and host/port.

## How the clip picking works

1. **Transcribe** with faster-whisper (word timestamps and voice activity detection) and cache the transcript.
2. **Measure** a loudness envelope (RMS every 0.5 s) and detect hard cuts (`ffmpeg` scene score).
3. **Pick**
   - *Claude:* the whole timestamped transcript and your length bounds go to Claude. It returns non-overlapping clips with a 0–100 score, title, hook, per-platform captions and hashtags, as JSON that must match a schema. Boundaries are snapped to words. The final score is 75 % Claude and 25 % local signals.
   - *Heuristic:* every sentence-aligned window inside your length bounds is scored on hook strength (first ~3 s), emotion, pace, speech density, loudness, cut rate, closeness to the target length and a clean ending. Then a greedy, non-overlapping selection runs.
4. **Render** each clip: one ffmpeg pass that trims, reframes, burns captions, normalises audio and encodes.

Scores predict how a clip compares with the *other clips from the same video*. Nobody can promise virality, so post several and keep what works.

## Rights and platform rules

Only clip and post content you own or have permission to use. Importing other people's YouTube videos and reposting them can break copyright law and platform terms. Each platform's API terms also apply to your app (rate limits, audit requirements, content rules).

## Development

```bash
pip install -e ".[all,dev]"
pytest
```

Layout:

```
clipforge/
  app.py              FastAPI routes + OAuth
  worker.py           background pipeline + posting scheduler
  pipeline/           media (ffmpeg), ingest, transcribe, candidates (heuristics), scoring (Claude),
                      reframe (face tracking), captions (ASS), render
  publishers/         youtube, tiktok, instagram, x
  static/             single-page UI
```

Platform APIs change often. Each publisher is one small file, so if an endpoint moves, that file is the only one to update.

## License

MIT
