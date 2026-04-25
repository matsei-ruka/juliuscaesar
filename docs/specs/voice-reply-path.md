# Voice Reply Path — Inbound audio → Outbound audio

**Status:** Spec (needs review)  
**Author:** Rachel  
**Date:** 2026-04-25  

## Problem

Voice inbound (via Telegram) is now transcribed and routed normally. The response is text. But a user who speaks to Rachel should hear her voice back, not read text.

Current state:
- ✅ Telegram voice message → detect, download OGG → DashScope ASR → transcript → queue
- ❌ Reply text → gateway routes, brains respond with text only
- ❌ No voice TTS path wired to gateway delivery

## Solution

When a reply is triggered by a voice message (meta.was_voice=True), synthesize audio via Rachel's cloned voice and deliver both text (fallback/transcript) and voice (primary) back to Telegram.

### High-level flow

```
User speaks (Telegram voice) 
  → TelegramChannel detects voice, downloads, transcribes to text
  → text event enqueued with meta.was_voice=True
  → gateway routes normally (triage, brain, etc)
  → brain returns text reply
  → deliver() checks meta.was_voice
    ├─ if True: call VoiceChannel.send(reply_text) → returns OGG path
    └─ TelegramChannel.send_voice(ogg_path) [NEW]
```

## Changes

### 1. `lib/gateway/channels/voice.py` — Fix TTS lookup and adapter

**Current problem:** `_synthesize` looks for non-existent `voice.dashscope_tts` module.

**Changes:**
- Load voice config from instance `voice/references/voice.json`
- Call `voice.synth.synthesize(text, out_path, voice_id=..., target_model=...)`
- Return OGG path on success; return None on failure
- Cache output path in temp dir; guarantee cleanup

**Signature:**
```python
def _synthesize(self, text: str, meta: dict[str, Any]) -> str | None:
    """Synthesize `text` with Rachel's cloned voice. Returns OGG path or None."""
    # Load voice/references/voice.json
    # Call voice.synth.synthesize
    # Return path
```

### 2. `lib/gateway/channels/telegram.py` — Add voice message send

**New method:**
```python
def send_voice(self, ogg_path: str, meta: dict[str, Any]) -> str | None:
    """Upload OGG file and send as voice message."""
    # GET ogg_path (local file)
    # POST to sendVoice with file upload
    # Return message_id on success
```

**Note:** Telegram `sendVoice` endpoint expects `multipart/form-data` with `voice` field (binary file). Different from `sendMessage`.

### 3. `lib/gateway/channels/registry.py` — Dual delivery for voice

**Current:** `deliver()` routes to ONE channel. Text and voice need both Telegram methods.

**Option A (simpler):** Keep single delivery. `TelegramChannel.send()` checks `meta.was_voice`:
- If True, calls `self.send_voice()` with OGG path (which must come from meta)
- If False, calls existing text method

**Option B (cleaner):** New `deliver_voice()` that takes (text_response, ogg_path) and routes both.

**Recommendation:** Option A. Keeps deliver() simple; TelegramChannel owns the multi-method logic.

### 4. `lib/gateway/runtime.py` — Trigger TTS before deliver

When processing replies with `meta.was_voice=True`:

```python
response = brain_result.response
if meta.get("was_voice"):
    voice_instance = self._get_voice_channel()  # cached VoiceChannel
    ogg_path = voice_instance.send(response, meta)
    if ogg_path:
        meta["synthesized_audio_path"] = ogg_path
```

Then deliver uses this path.

### 5. Tests

**New tests in `tests/gateway/test_channels.py`:**

- `test_voice_send_returns_path_on_success`: mock `voice.synth.synthesize`, assert TTS path returned
- `test_voice_send_returns_none_on_missing_config`: missing `voice.json` → graceful None
- `test_telegram_send_voice_uploads_and_returns_message_id`: mock Telegram API, assert `sendVoice` called with file upload

## Contracts

### `voice.json` format (instance-specific)
```json
{
  "voice": "qwen-tts-vc-rachel-voice-...",
  "target_model": "qwen3-tts-vc-realtime-2026-01-15",
  "preferred_name": "rachel"
}
```
Already created by `jc voice enroll`. No change.

### `meta.was_voice` flag
Set by TelegramChannel during inbound voice ingestion (already implemented). Consumed by runtime and deliver.

### Telegram `sendVoice` response
```json
{
  "ok": true,
  "result": {
    "message_id": 4906,
    "chat": {...},
    "voice": {...},
    ...
  }
}
```

## Error handling

- **ASR failed:** current behavior (empty transcript), event skipped. No change.
- **TTS failed:** voice_instance.send returns None → deliver sends text only (graceful degradation).
- **Telegram upload failed:** `send_voice` raises RuntimeError → event marked failed, retried. No silent skip.

## Non-goals

- TTS for text-only replies. Only replies triggered by voice.
- TTS model choice. Always uses Rachel's enrolled voice_id + target_model.
- Audio transcoding. Input OGG from TTS, Telegram accepts it directly.

## Timeline

Estimate: 2–3 hours (TTS adapter + dual-send methods + tests).

Blocks: None. Additive to existing voice ingestion.

## Review checklist

- [ ] Agree on Option A (TelegramChannel owns multi-method) vs Option B
- [ ] Voice config loading location OK (voice/references/voice.json)
- [ ] Error handling strategy (graceful fallback to text)
- [ ] Test approach sufficient
