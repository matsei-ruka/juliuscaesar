# ASR Whisper Fallback — DashScope primary, Whisper backstop for long audio

**Status:** Spec (needs review)
**Author:** Rachel
**Date:** 2026-06-03

## Problem

`lib/voice/asr.py` calls DashScope `qwen2.5-omni-7b` to transcribe inbound voice. The model hard-caps audio at ~60s. Real evidence today: Luca sent a 3m41s voice message and the call returned:

```
400 InvalidParameter: The audio is too long
```

The event was dropped. From Luca's side: silence. We need a transparent fallback so long voice messages still transcribe without the user noticing, while DashScope stays the default for the short-message common case (it's cheaper and already wired with the cloned-voice / multimodal stack).

## Solution

Keep DashScope as the primary path. On the specific "too long" failure mode, fall back to OpenAI Whisper (`whisper-1`) for that one call. Caller signature unchanged — `transcribe()` returns a string or raises.

### High-level flow

```
transcribe(audio_path, ...)
  → POST DashScope qwen-omni
    ├─ 200 OK → return text   (unchanged)
    ├─ 400 "audio is too long" / "InvalidParameter"
    │     → log INFO "asr fallback: dashscope→whisper reason=<short>"
    │     → POST OpenAI Whisper multipart
    │         ├─ 200 OK → return text
    │         └─ any failure (incl. missing key) → re-raise ORIGINAL DashScope RuntimeError
    └─ other failure (auth, network, 5xx) → re-raise (NO fallback)
```

The fallback is narrow on purpose: it only fires on the specific symptom that motivated it. Any other DashScope error keeps current behavior (raise → gateway logs → retry per existing policy).

## Changes

### 1. `lib/voice/asr.py` — single-file change

Refactor `transcribe()` so the DashScope call is the primary path; on the targeted error symptom, route to a new private `_transcribe_whisper()` helper. No other files change. Public signature stays:

```python
def transcribe(
    audio_path: Path,
    *,
    instance_dir: Path,
    model: str = DEFAULT_MODEL,
    prompt: str = DEFAULT_PROMPT,
    url: str = URL_INTL,
    timeout_s: float = 120.0,
) -> str: ...
```

New constants:
```python
WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-1"
FALLBACK_TRIGGERS = ("audio is too long", "InvalidParameter")
```

New private helper:
```python
def _transcribe_whisper(
    audio_path: Path,
    *,
    instance_dir: Path,
    timeout_s: float,
) -> str:
    """POST raw audio bytes to OpenAI Whisper, multipart. Returns text.

    Raises RuntimeError on missing key or any HTTP/transport failure.
    """
```

Implementation notes:
- `api_key = env_value(instance_dir, "OPENAI_API_KEY")` — missing → raise RuntimeError.
- Multipart form: `files={"file": (audio_path.name, audio_path.read_bytes(), mime)}`, `data={"model": "whisper-1", "response_format": "text"}`.
- Header: `Authorization: Bearer <key>`. No `Content-Type` (requests sets multipart boundary).
- File is sent as raw bytes — NOT base64 (Whisper API expects multipart binary).
- `response_format=text` → Whisper returns plain text, not JSON. Use `r.text.strip()`.
- Non-200 → `raise RuntimeError(f"whisper failed: {r.status_code} {r.text[:500]}")`.

Refactor in `transcribe()`:
- Wrap the existing DashScope HTTP call's error path in a check: if `r.status_code != 200`, build the same `RuntimeError` as today (`f"transcription failed: {r.status_code} {r.text[:500]}"`), then before raising decide whether to fall back.
- Fallback decision: if any of `FALLBACK_TRIGGERS` substring-matches in `r.text`, attempt Whisper.
- If Whisper succeeds → return its text.
- If Whisper fails for any reason (missing key, HTTP error, transport) → log WARN with the Whisper reason for debuggability, then raise the **original** DashScope `RuntimeError` (not the Whisper one). This keeps gateway logs intelligible — a single canonical error per inbound event.

### 2. Logging

Use stdlib `logging` like the rest of `lib/voice/` (see `lib/voice/synth.py` pattern). Module-level `logger = logging.getLogger(__name__)`.

- INFO at fallback start: `"asr fallback: dashscope→whisper reason=<short>"` where `<short>` is `audio_too_long` or `invalid_parameter` depending on which trigger matched.
- WARN if Whisper itself fails before re-raising the original error: `"asr fallback whisper failed: <reason>"`.

No DEBUG-level dumps of audio bytes or full response bodies (we already truncate to 500 chars in the raised string).

### 3. `tests/voice/test_asr.py` — new file

Mirror the style of `tests/voice/test_env_lookup.py`. Mock `requests.post` with `unittest.mock.patch`. No network. Use `tmp_path` for a dummy OGG file (small bytes blob is fine — neither DashScope nor Whisper actually run).

Helper:
```python
def _make_audio(tmp_path):
    p = tmp_path / "msg.ogg"
    p.write_bytes(b"\x00\x01\x02fake-ogg-bytes")
    return p
```

Each test stubs `env_value` to return known keys, and stubs `requests.post` with a `side_effect` callable that inspects `args[0]` (the URL) to decide which response to return — DashScope vs Whisper.

#### Test cases

1. **`test_dashscope_success_no_fallback`**
   - DashScope returns 200 with valid omni payload → `transcribe()` returns DashScope text.
   - Assert `requests.post` called exactly once, with the DashScope URL.

2. **`test_fallback_on_audio_too_long_whisper_succeeds`**
   - DashScope returns 400, body contains `"The audio is too long"`.
   - Whisper returns 200, text body `"long audio transcript"`.
   - `transcribe()` returns `"long audio transcript"`.
   - Assert two POSTs: DashScope then Whisper.
   - Assert Whisper call used multipart (`files=` kwarg present), `data["model"] == "whisper-1"`, `data["response_format"] == "text"`, `Authorization: Bearer <openai-key>` header.

3. **`test_fallback_on_invalid_parameter_whisper_succeeds`**
   - DashScope returns 400, body contains `"InvalidParameter"` (no "audio is too long" substring).
   - Whisper 200 → returns Whisper text.
   - Same multipart assertions.

4. **`test_fallback_but_no_openai_key_raises_original`**
   - DashScope returns 400 "audio is too long".
   - `env_value(..., "OPENAI_API_KEY")` returns empty string.
   - Whisper is NOT called (assert second POST never happens).
   - `RuntimeError` raised, message starts with `"transcription failed: 400"` (the DashScope error, NOT a Whisper error).

5. **`test_fallback_but_whisper_500_raises_original`**
   - DashScope 400 "audio is too long".
   - Whisper returns 500 with `"internal error"`.
   - `RuntimeError` raised, message starts with `"transcription failed: 400"` (DashScope error, not `"whisper failed: 500"`).

6. **`test_dashscope_auth_failure_no_fallback`**
   - DashScope returns 401, body `"Unauthorized"`.
   - `requests.post` called once (DashScope only).
   - `RuntimeError` raised with `"transcription failed: 401"`.

7. **`test_dashscope_500_no_fallback`**
   - DashScope returns 500 with body `"Internal Server Error"` (no trigger substring).
   - One POST only. Raise.

8. **`test_dashscope_network_error_no_fallback`** (optional, judgement call)
   - `requests.post` raises `requests.exceptions.ConnectionError` on the DashScope call.
   - That error propagates as-is. Whisper not called.

## Contracts

### `OPENAI_API_KEY`
Read via `env_value(instance_dir, "OPENAI_API_KEY")`. Empty / missing → no fallback possible for this instance; behavior is identical to today (raise the DashScope error). Already added to Rachel's `.env` manually — no install-step change required.

### Whisper request shape
```
POST https://api.openai.com/v1/audio/transcriptions
Authorization: Bearer <OPENAI_API_KEY>
Content-Type: multipart/form-data; boundary=...   (set by requests)

--boundary
Content-Disposition: form-data; name="file"; filename="msg.ogg"
Content-Type: audio/ogg
<raw bytes>
--boundary
Content-Disposition: form-data; name="model"
whisper-1
--boundary
Content-Disposition: form-data; name="response_format"
text
--boundary--
```
Response on success: HTTP 200, `Content-Type: text/plain`, body is the transcript.

### Whisper limits (for context)
- Hard cap: 25 MB file size. A 3m41s OGG/Opus voice message from Telegram is ~600 KB — well under. We don't pre-check; if Whisper rejects on size, the error propagates per the "Whisper fails → raise original DashScope error" rule.
- Whisper supports all common audio formats including OGG/Opus directly. No transcoding.

## Error handling matrix

| DashScope result | Trigger substring? | Whisper result | `transcribe()` returns / raises |
|---|---|---|---|
| 200 OK | — | not called | DashScope text |
| 400 "audio is too long" | yes | 200 OK | Whisper text |
| 400 "InvalidParameter" | yes | 200 OK | Whisper text |
| 400 "audio is too long" | yes | missing OPENAI_API_KEY | RAISE DashScope error |
| 400 "audio is too long" | yes | 5xx / transport error | RAISE DashScope error (log Whisper reason as WARN) |
| 401 / 403 / 5xx | no | not called | RAISE DashScope error |
| 400 with other body | no | not called | RAISE DashScope error |
| network error | n/a | not called | RAISE the original requests exception |

## Non-goals

- **Picking the cheaper provider based on duration.** Always try DashScope first. Whisper is a backstop, not a primary path.
- **Streaming / chunked transcription.** Whisper handles up to 25 MB in one call — fine for Telegram voice notes.
- **Language hinting.** Whisper auto-detects. DashScope prompt already says "in the spoken language". No `language` param passed.
- **Caching transcripts.** Out of scope for this spec.
- **Whisper rate-limiting / retry policy.** Single attempt. If it fails, we fall through to raising the original error.
- **Touching callers** (`lib/gateway/runtime.py`, channels). Signature and exception type unchanged.

## Timeline

Spec review: today. Implementation + tests after approval: ~1 hour.

Blocks: none. Additive logic inside one function. No schema, no config, no migration.

## Review checklist

- [ ] Fallback trigger set OK — substring match on `"audio is too long"` and `"InvalidParameter"`. Any others worth including?
- [ ] Re-raising the **original DashScope error** when Whisper fails — agree this is right (vs. a combined error)?
- [ ] `response_format=text` (Whisper returns plain text) vs `json` (would also give us language detection) — text is simpler, no parsing. Agree?
- [ ] Logging level: INFO on fallback trigger, WARN on Whisper failure. Sound right or too chatty?
- [ ] Test list complete? Anything to add (e.g. weird DashScope payloads that still parse but contain empty `text`)?
