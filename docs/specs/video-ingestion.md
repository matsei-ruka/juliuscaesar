---
slug: video-ingestion
title: Video ingestion — split-and-fuse (audio → ASR, frames → qwen3.6-plus)
status: draft
owner: rachel
target: feat/video-ingestion
created: 2026-05-16
---

# Video ingestion — split-and-fuse

## Goal

When an instance receives a Telegram `video` attachment (full clip, not just `video_note`/`voice`), produce **two derived artifacts**:

1. **Transcript** — strip audio, run through the existing voice ASR pipeline (DashScope qwen2.5-omni). Same path as voice messages.
2. **Visual description** — pass the video file to **qwen3.6-plus** via the DashScope int'l compatible-mode endpoint, with a prompt that asks for visual narrative only (no transcript guessing).

Both artifacts are then **fused** into the brain prompt as a single virtual event:

```
[user sent a video; transcript and visual analysis follow]
TRANSCRIPT:
{asr_output}

VISUAL:
{vlm_output}
```

## Why this design

Empirical benchmark on a 1.6 MB Italian video (`348659633.mp4`):

| Model              | Transcript     | Visual      |
|--------------------|----------------|-------------|
| qwen3.5-omni-flash | ✅ accurate    | ❌ wrong finger count |
| qwen3.6-plus       | ❌ hallucinated | ✅ correct (3 fingers, scene, gesture) |
| qwen-vl-plus       | ❌ hallucinated | ✅ correct (3 fingers) |

Conclusion: no single Qwen model does both jobs well. The omni model is audio-trained; the plus/vl models are vision-trained and confabulate audio from frames. Splitting the job aligns each artifact with the model that actually does it.

## Scope

### In scope

- Telegram channel — accept `video` payload (mp4) in addition to existing `voice`/`audio`/`video_note`.
- New module `lib/voice/video.py` (or extend `lib/voice/`) that:
  - Strips audio to a temp wav/mp3 via `ffmpeg -i <in> -vn -ac 1 -ar 16000 <out>.wav`.
  - Calls the existing `asr.transcribe(audio_path)` on the stripped audio.
  - Calls a new `vlm.describe_video(video_path)` that POSTs to DashScope int'l compatible-mode `chat/completions` with `model=qwen3.6-plus` and base64 `video_url`.
  - Returns `(transcript: str, visual: str)`.
- Event meta — add `transcript` and `visual` fields; preamble builder fuses them into the brain prompt.

### Out of scope (this spec)

- Slack/Discord/email video ingestion (Telegram first; same module reusable later).
- Streaming/chunked video for long clips (initial version: hard cap 50 MB, fail fast).
- TTS reply-as-video (audio-only TTS reply continues to be the default).

## Module layout

```
lib/voice/
  asr.py         (existing — unchanged)
  synth.py       (existing — unchanged)
  enroll.py      (existing — unchanged)
  video.py       (NEW — ingest_video(path) -> (transcript, visual))
  vlm.py         (NEW — describe_video(path, prompt) -> str)
```

## Key prompt for `vlm.describe_video`

```
Describe the visual content of this video. Cover:
- Subject(s): appearance, clothing, expression.
- Setting: background, lighting, notable objects.
- Action: camera movement, gestures, key actions.

Do NOT transcribe the audio. Do NOT guess what is being said.
Output: 2–4 sentence prose paragraph.
```

The "do NOT transcribe" guard is the critical anti-confabulation lever (the plus/vl models default to making up speech otherwise).

## Configuration

`ops/gateway.yaml`:

```yaml
voice:
  enabled: true
  asr_provider: dashscope
  tts_provider: dashscope
  # NEW
  video:
    enabled: true
    vlm_model: qwen3.6-plus       # opt out by setting "" or null
    max_size_mb: 50
    transcript_when_silent: ""    # what to emit when audio strip yields no speech
```

Per-instance opt-out via `voice.video.enabled: false`.

Env vars: same `DASHSCOPE_API_KEY` already used by ASR. No new credentials.

## Failure modes

- **No ffmpeg on host** → log error, skip audio strip, deliver visual-only event.
- **Audio strip yields silence** → ASR returns "", emit `transcript: ""` (preamble omits the section).
- **VLM call fails** → fall back to visual-empty event; transcript still flows.
- **File >50 MB** → reject at channel layer with a polite "video too large" reply.

## Acceptance tests

1. Ingest `348659633.mp4` → event meta contains:
   - `transcript`: matches the Italian original ±5% character distance.
   - `visual`: mentions "bald", "beard", "glasses", "three fingers" (substring match).
2. Ingest silent video (audio track muted) → `transcript: ""`, `visual` populated.
3. Ingest >50 MB clip → channel rejects with operator-configured message, no DashScope call.
4. `voice.video.enabled: false` → channel falls back to current behavior (event with raw file path, no transcript / visual).

## Open questions

- **Spec for non-Latin scripts.** ASR handles them; need to confirm preamble formatting renders Arabic/Chinese inline. Should not block initial ship.
- **Frame sampling rate.** qwen3.6-plus seems to internally sample frames; we don't pass an explicit FPS. If long clips degrade, expose a `frames_per_second` param.
- **Cost ceiling.** ~1,800 tokens per 30-second clip at current pricing. Add a per-instance per-day token budget? Out of scope for v1; add to runbook.

## Rollout

1. Branch: `feat/video-ingestion`.
2. Implement `lib/voice/video.py` + `lib/voice/vlm.py`.
3. Extend `lib/gateway/channels/telegram.py` (`video` payload accepted alongside `video_note`).
4. Tests: unit (mock DashScope), integration (live `348659633.mp4`).
5. Ship behind `voice.video.enabled` flag. Default **off** for v1; flip on per instance after smoke.
