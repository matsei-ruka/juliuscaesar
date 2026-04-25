# Telegram Multimedia Ingestion + Typing Indicator

**Status:** Spec
**Author:** Rachel
**Date:** 2026-04-25
**Branch:** `feat/telegram-multimedia`

## Goals

1. Ingest non-voice Telegram media: `audio` (music/podcasts), `video_note` (round videos), `photo`, `document`.
2. Forward-aware logging — if `forward_from` / `forward_from_chat` set, record it for audit but treat as normal message.
3. Route image events to a vision-capable brain (claude → gemini fallback) with the local file path passed through `meta`.
4. Hand documents to the brain with file path + caption — let the brain decide what to do (PDF, OCR, etc.) — no special pipeline.
5. Show a Telegram **typing** indicator while a brain is processing, refreshed every 4s, capped at 60s.

## Non-goals

- OCR / PDF parsing pipeline. The brain owns content extraction for documents.
- Sticker / animation / dice / contact / location / poll ingestion.
- Multi-photo album grouping (`media_group_id`). Each photo enqueues its own event.
- Audio transcoding. ASR consumes whatever Telegram serves.
- Persisting media beyond the inbound directory. Cleanup is out-of-scope.

## API surface

### `lib/gateway/channels/telegram.py`

- New ingestion branches inside `TelegramChannel.run` for `audio`, `video_note`, `photo`, `document`. They share the existing `voice` flow as much as possible.
- New helpers (private):
  - `_ingest_audio(payload, update_id) -> Path` — reuses `_download_telegram_file`, MIME-mapped extension, saves to `state/voice/inbound/<update_id>.<ext>`.
  - `_ingest_video_note(payload, update_id) -> Path` — same destination tree, `.mp4` extension.
  - `_ingest_photo(photos, update_id) -> Path` — picks `photos[-1]` (largest), saves to `state/voice/inbound/photos/<update_id>.jpg`.
  - `_ingest_document(payload, update_id) -> Path` — preserves original filename's extension; saves to `state/voice/inbound/docs/<update_id><ext>`. Uses `original_file_name` if present.
- New public method:
  - `send_typing(chat_id: str, message_thread_id: int | None = None) -> None` — POST `sendChatAction` with `action=typing`. Best-effort, swallows errors.
- Extend `_VOICE_MIME_EXT` (renamed/extended set) to include audio MIME types: `audio/webm`, `audio/mp4`, `audio/x-m4a`. Keep the existing fallback `.oga` for unknown audio mimes. Video_note uses a hard-coded `.mp4`.
- Forward detection: when `message.forward_from` or `message.forward_from_chat` present, log a single line `telegram forward update_id=… from=…`. Continue normal ingestion.

### `lib/gateway/runtime.py`

- `process_event` spawns a typing thread before routing/brain when `meta.delivery_channel == "telegram"` (or source is telegram and no override). Thread receives a `threading.Event` (`_stop_typing`); main path sets the event after `deliver()`.
- Thread implementation: emit `send_typing` immediately, then loop `event.wait(4)` until set or 60 s elapsed; each iteration re-emits `send_typing`. Catches all exceptions, silently.
- Vision routing: when `meta.image_path` is set, force-route to `claude` (override `selection.brain`) unless an explicit `brain_override` / sticky / triage already chose claude or gemini. If claude adapter validation fails, fall back to `gemini`. Pass `image_path` and `file_path` through to the brain via `meta` (already serialized into `event.meta`); the brain's prompt template already echoes meta into the prompt body.

### `lib/gateway/brains/claude.py`

No code change. Claude adapter already accepts the prompt verbatim and Claude Code itself has Read access to the local FS. We only need to make sure the path appears in the prompt — `Brain.prompt_for_event` already serializes meta as JSON in the prompt body, so `meta.image_path` and `meta.file_path` reach Claude.

## Data flow

```
Telegram update
 ├─ message.text/caption only         → enqueue text (existing)
 ├─ message.voice (no text)           → existing OGG → ASR → enqueue
 ├─ message.audio (no text)           → download via getFile → ASR → enqueue with audio_path
 ├─ message.video_note (no text)      → download → ASR → enqueue with audio_path
 ├─ message.photo (any)               → download largest → enqueue meta.image_path + caption
 └─ message.document (any)            → download → enqueue meta.file_path + caption

Runtime.process_event(event)
 ├─ start typing thread (telegram)    → sendChatAction every 4s, ≤ 60s
 ├─ if meta.image_path: prefer claude (gemini fallback)
 ├─ invoke brain (path inside meta → reaches prompt as JSON)
 ├─ render voice reply if was_voice
 ├─ deliver()
 └─ stop typing thread
```

Storage layout:

```
state/voice/inbound/
  ├─ <update>.oga / .mp3 / .m4a / .webm   (voice + audio)
  ├─ <update>.mp4                          (video_note)
  ├─ photos/<update>.jpg                   (photo)
  └─ docs/<update><ext>                    (document, ext from original)
```

## Failure modes

| Failure | Behavior |
|---------|----------|
| `getFile` HTTP error | log + skip event (no enqueue) |
| Download stream fails | log + skip event |
| ASR returns empty | log + skip (matches existing voice path) |
| Photo download fails | log; if caption non-empty, fall through to enqueue text-only (no `image_path`); else skip |
| Document download fails | same fallback as photo: keep caption-only event |
| `sendChatAction` fails | silent — never crash event processing |
| Typing thread exception | silent — daemon thread, swallows everything |
| Vision brain unreachable | fall back to gemini; if both invalid, default routing applies (claude attempt fails loudly via existing adapter exit code) |

## Test plan

`tests/gateway/test_channels.py`:

1. `test_audio_message_is_transcribed_and_enqueued` — clones the existing voice test using `message.audio` payload (mime `audio/mpeg` → `.mp3`), asserts transcript + `audio_path` enqueued.
2. `test_video_note_is_transcribed_and_enqueued` — `message.video_note`, asserts transcript + `audio_path` (`.mp4`).
3. `test_photo_largest_size_saved_with_caption` — three photo sizes, largest selected, file lands under `state/voice/inbound/photos/`, `meta.image_path` set, `content` is the caption.
4. `test_document_saved_with_meta_file_path` — `original_file_name=report.pdf`, lands under `state/voice/inbound/docs/`, extension `.pdf`, `meta.file_path` set.
5. `test_forward_detection_logs_and_proceeds` — message has `forward_from`, asserts log line emitted and event still enqueued.
6. `test_send_typing_posts_chat_action` — direct unit test on `TelegramChannel.send_typing`, mocks `http_json`, asserts `/sendChatAction` URL + `action=typing` body.

`tests/gateway/test_typing.py` (new file — runtime-level):

7. `test_typing_thread_calls_immediately_and_at_4s` — fake clock or `monkey-patched` `event.wait` records call times.
8. `test_typing_thread_caps_at_60s` — ensure loop exits ≤ 60s even if stop event never set.
9. `test_typing_thread_silent_on_send_failure` — `send_typing` raises every call; thread does not propagate.

(Test 7–9 will live in `test_channels.py` to keep the test layout small — single file already covers all gateway channel tests.)

## Open questions

None — defaults documented above. Vision fallback to gemini matches existing brain registry order.

## Timeline

~3 h: ingestion branches + helpers (1 h), runtime typing thread (1 h), tests (1 h).
