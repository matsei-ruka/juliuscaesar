---
title: Voice gateway channel
section: subsystem
status: active
code_anchors:
  - path: lib/gateway/channels/voice.py
    symbol: "class VoiceChannel:"
last_verified: 2026-05-01
verified_by: l.mattei
related:
  - subsystem/gateway-queue.md
  - subsystem/voice-dashscope.md
---

## Summary

The `voice` channel does not own a transport. It pairs with another I/O
channel (default `telegram`) and offers ASR/TTS hooks delegated to
`lib/voice/` (DashScope).

The paired channel is responsible for receiving audio attachments and pushing
them at the gateway as voice events with `meta.audio_path` set. The runtime
uses the voice channel's `transcribe()` helper to turn the audio into text
before invoking the brain. On the way out, the channel renders a TTS audio
reply that the paired channel ships alongside the text.

## Configuration

```yaml
channels:
  voice:
    enabled: true
    paired_with: telegram         # telegram | slack | discord
    asr_provider: dashscope
    tts_provider: dashscope
```

`DASHSCOPE_API_KEY` must be set in `<instance>/.env`.

## Invariants

- Voice never owns a transport — disabling the paired channel disables voice.
- Failed ASR/TTS log and continue with text-only fallback.
