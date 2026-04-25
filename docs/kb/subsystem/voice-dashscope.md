---
title: DashScope voice subsystem
section: subsystem
status: active
code_anchors:
  - path: bin/jc-voice
    symbol: "Subcommands:"
  - path: lib/voice/synth.py
    symbol: "def synthesize("
  - path: lib/voice/asr.py
    symbol: "def transcribe("
  - path: lib/voice/enroll.py
    symbol: "def enroll("
last_verified: 2026-04-25
verified_by: l.mattei
related:
  - contract/config-and-secret-boundaries.md
---

## Summary

Voice support wraps DashScope Qwen APIs for three operations: enroll a cloned voice, synthesize speech as Telegram-compatible OGG/Opus, and transcribe audio through qwen2.5-omni.

The CLI handles instance resolution and `.env` loading. Library functions take explicit parameters and read `DASHSCOPE_API_KEY` from the environment.

## CLI surface

`jc voice` supports:

- `speak "text" [--out path.ogg]`
- `transcribe <audio-file> [--out text.txt]`
- `enroll <audio-sample> [--name NAME] [--target-model MODEL]`
- `list-voices`

## Files

- Voice metadata: `<instance>/voice/references/voice.json`
- Default generated speech: `<instance>/voice/tmp/out.ogg`
- Templates: `templates/init-instance/voice/`

## Implementation notes

- Enrollment calls the DashScope customization endpoint and returns `{voice, target_model, preferred_name}`.
- Synthesis uses the international realtime WebSocket endpoint, writes raw PCM to a temporary file, then converts it with ffmpeg to OGG/Opus.
- Transcription base64-embeds the audio file in a multimodal generation request.

## Invariants

- `DASHSCOPE_API_KEY` is required for all voice operations.
- `ffmpeg` is required for synthesis.
- Missing `voice.json` blocks `speak` and tells the user to enroll first.

## Open questions / known stale

- 2026-04-25: Voice is DashScope-only. No alternate provider abstraction exists yet.
