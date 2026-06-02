# Spec: Even Realities G2 Glasses ↔ JC Bridge

**Status:** Draft
**Date:** 2026-06-01
**Branch base:** `main`
**Branch:** `spec/g2-glasses-bridge`
**Owner:** TBD

## Goal

Let a Luca-class operator talk to **any JC agent** (Harold, Rachel, future personas) from Even Realities G2 smart glasses, by voice, with no arbitrary latency cutoff and without handing third-party apps the operator's Telegram credentials.

The glasses become a thin client to the operator's existing JC agent fleet:

1. Operator picks the active agent once from a menu on the glasses (Harold / Rachel / future personas). Selection persists across launches.
2. Operator speaks into the G2 mic.
3. Audio streams as raw PCM (16 kHz mono s16le) to a self-hosted Python bridge over WebSocket.
4. Bridge encodes the utterance to OGG/Opus in memory and forwards it as a Telegram **voice note** to the selected agent's bot DM, **as the operator's own Telegram user account**, using Telethon (MTProto user API — not bot API). Caption: `[sent from even G2]`.
5. JC's existing voice channel transcribes (Dashscope `qwen2.5-omni-7b`), triages, brains, and replies — exactly as it does for any other voice message. **No JC changes.**
6. Agent replies in its usual conversation; the bridge mirrors the reply back to the glasses display over the same WebSocket and renders it.
7. No 20-second deadline. If the agent takes 90 s, the glasses display the answer when it arrives.

The bridge is the only new piece. The existing JC gateway, persona instances, voice pipeline, and Telegram conversations are unchanged.

## Non-goals

- Do not reuse, repackage, or fork the OpenClaw Even-G2 bridge (`dAAAb/openclaw-even-g2-bridge-skill`). Its 22-second hard deadline and single-agent routing model are explicit anti-requirements here.
- Do not use the G2 stock "Even AI" app or its OpenAI-compatible chat-completions configuration. We build our own Even Hub app so we control mic capture, display rendering, and the wire protocol end to end.
- Do not use a Telegram **bot** account for routing. Operator wants the glasses to speak as *himself* into the existing 1:1 agent conversations (chat 28547271 ↔ Harold, etc.), not as a third party. This is non-negotiable.
- Do not transcribe on the glasses. Glasses send raw PCM; the bridge encodes to OGG/Opus and forwards as a Telegram voice note; JC's existing voice channel transcribes (Dashscope `qwen2.5-omni-7b`).
- Do not modify the JC gateway, persona brains, or Telegram channel adapter for this feature. The bridge is fully external to JC — it talks to JC the same way a phone does.
- Do not introduce a new agent-addressing syntax inside the JC framework. The bridge resolves the active agent → Telegram chat ID locally and sends to that chat. JC sees normal Telegram messages.
- Do not parse the transcript for an agent name. Agent selection is **menu-driven on the glasses** (operator picks once, until they pick another) — not "Harold, do X" inline.
- Do not maintain a static agent registry in config. The agent list is **discovered live from the operator's Telegram bot dialogs** via Telethon.
- Do not require Docker. Native Python + a single Even Hub app bundle. systemd unit for the bridge.

## Background

### G2 hardware + SDK (verified 2026-06-01)

Source: `hub.evenrealities.com/docs`, `@evenrealities/even_hub_sdk` (npm), and SDK feature-verification reports from third-party developers.

| Capability | Status | Detail |
|---|---|---|
| Raw mic audio | **Confirmed** | `audioControl(true)` enables mic. PCM streams via `audioEvent.audioPcm` as `Uint8Array`. Format: **16 kHz mono 16-bit signed little-endian**. Requires `g2-microphone` permission in `app.json`. |
| Text on display | **Confirmed** | `TextContainerProperty` + `textContainerUpgrade` render text with flicker-free partial updates. Display: 576×288 px per eye, 4-bit greyscale. `textContainerUpgrade` accepts up to **2,000 chars** per update; **~400–500 chars fill a full-screen container** at default font (per Even Hub Display docs). Implementer picks the page size that's readable on hardware — typical voice replies are short enough not to need pagination at all. |
| Touch input | **Confirmed** | Click, double-click, scroll on left/right temple + R1 ring. Use for scroll-through-paginated-reply and dismiss. |
| Outbound networking | **Confirmed** | `fetch()`, `XMLHttpRequest`, WebSocket from the app WebView. Two gates: (a) domains whitelisted in `app.json`, (b) standard CORS from server side. Binary frames over WebSocket are supported. |
| OS-level push notification (app closed) | **Not in SDK** | No documented push API to wake the app or post to a system notification tray. **Worked around** by holding a persistent WebSocket from the app to the bridge while the app is foregrounded; for background, see "Open decisions" below. |
| Speaker | **Absent** | G2 has no speaker. TTS does not apply to this path. Display-only output. |
| Image on display | **Confirmed** | 1-bit / 4-bit grayscale images via container API. Not required for v1 but useful later (e.g., infographic preview). |

A simulator exists: `BxNxM/even-dev` (Even Realities Hub Simulator — multi-app test environment). Development and CI tests can run against the simulator before deploying the bundle to physical G2.

### JC current state (relevant pieces)

- `lib/voice/asr.py` — Dashscope `qwen2.5-omni-7b` REST transcription. Measured ~1.5 s per request on a typical 5-second voice note (benchmark 2026-06-02 on Luca's host). Handles English and Italian. Used by the existing Telegram voice path.
- `lib/voice/synth.py` — Dashscope `QwenTtsRealtime` WebSocket TTS, blocks on `session.finished`. Not relevant to the glasses path (no speaker on G2) but documented here so an implementer doesn't mistake it for a dependency.
- `ops/gateway.yaml` — channels stanza already includes `voice`, `jc-events`, `cron`. The bridge does **not** add a new channel adapter to JC. It impersonates the operator's Telegram user via Telethon and posts into the existing `telegram` channel conversations.
- `memory/L1/CHATS.md` per instance — authoritative source of "which conversation belongs to which agent" from the operator's POV. The bridge consumes a derived form of this (see "Agent registry").

### Why a bridge that posts as the operator's Telegram user

Three options were considered:

1. **JC HTTP ingress** — add an HTTP endpoint to the JC gateway and have the bridge POST messages there as a new channel. Rejected: requires invasive framework changes; replies would not appear in the operator's normal Telegram thread; multi-instance fleet needs N ingresses.
2. **Telegram bot account** — bridge sends as a bot. Rejected: agents would not see the operator as themselves; conversation context fragmented; bot rate-limits and message-edit rules differ.
3. **Telethon (MTProto user API), self-hosted** — bridge logs in once as the operator and posts/listens as the operator. **Chosen.** Reply appears in the existing agent thread. JC sees nothing new. Credentials never leave the operator's server.

The third-party "OpenClaw" route was also rejected explicitly by the operator on grounds of (a) 22 s deadline and (b) handing Telegram credentials / session to an external service.

## Architecture

```
+----------------+     BLE      +-------------+   WiFi/4G    +-------------------+
|     G2 mic     | ===========> | Even Hub    | ===========> |  Bridge (Python)  |
|  audioPcm 16k  |              |  custom app |   WebSocket  |  - WS server      |
+----------------+              |  (TS/HTML)  | <=========== |  - PCM→OGG/Opus   |
                                |             |   text+meta  |  - Agent select   |
+----------------+              |             |              |  - Telethon       |
|  G2 display    | <=========== |             |              +----------+--------+
|  text/scroll   |              +-------------+                         |
+----------------+                                                      | Telegram MTProto
                                                                        | (as operator user)
                                                                        | sendVoiceMessage(.ogg)
                                                                        v
                                                       +-----------------------------------+
                                                       | Telegram (operator's account)     |
                                                       |  • DM with Harold (chat 28547271) |
                                                       |  • DM with Rachel (chat ...)      |
                                                       |  • DM with <future agents>        |
                                                       +-----------------+-----------------+
                                                                         |
                                                                         v
                                                       Existing JC gateway receives the
                                                       voice message, runs ASR via
                                                       lib/voice/asr.py, tags it as
                                                       [glasses] (operator self-send
                                                       convention), triages, brains,
                                                       replies as today.
```

### Three actors

1. **Even Hub app** — small custom TypeScript/HTML bundle running in the Even Hub WebView on the operator's phone. Captures mic, streams PCM frames over WebSocket, receives reply frames, renders text.
2. **Bridge** — Python daemon on the operator's server. Speaks WebSocket south (to the app) and Telethon-MTProto north (to Telegram). **Does NOT transcribe.** Encodes the buffered PCM into an Opus voice note and forwards it via Telethon to the selected agent's Telegram bot DM.
3. **JC fleet** — **completely unchanged.** The existing voice channel (`lib/voice/asr.py` → triage → brain) handles transcription, model routing (Haiku / Sonnet / Opus), and reply. The bridge sends a voice note with a text caption `[sent from even G2]`; the agent receives a standard Telegram voice message. No JC code changes required.

### Why ASR lives on JC (operator decision 2026-06-02)

- Bridge stays thin. No `DASHSCOPE_API_KEY` on the bridge host, no ASR adapter to maintain, no drift versus JC's evolving ASR.
- JC's voice pipeline is already proven and currently configured. Same code path used by every other voice channel.
- Voice notes are archived in the operator's Telegram thread: searchable later, audit trail, no separate retention to manage.
- Latency cost is negligible: voice-note upload to Telegram adds ~200 ms; ASR happens in JC the same way it always does. Measured ASR step alone is ~1.5 s.

## Components

### Component A — Even Hub app (`apps/jc-glasses/`)

**Tech:** TypeScript + Even Hub SDK + minimal HTML.
**Manifest (`app.json`):** declare permissions `g2-microphone`, networking whitelist for the bridge's WSS domain.
**Responsibilities:**

- Open and hold a single WebSocket to the bridge. Reconnect with exponential backoff on drop.
- **Mic toggle = single `CLICK_EVENT` on the left temple** (`eventSource = TOUCH_EVENT_FROM_GLASSES_L`). First click: call `audioControl(true)`, send `mic_on` frame, status → `LISTEN`. Second click: call `audioControl(false)`, send `mic_off` frame, status → `THINK`. No long-press / press-and-hold (not exposed as an SDK event — confirmed against Even Hub `Input & Events` doc, only `CLICK_EVENT`, `DOUBLE_CLICK_EVENT`, `SCROLL_TOP_EVENT`, `SCROLL_BOTTOM_EVENT` are documented).
- **Silence safety net.** If the bridge sees ≥ 3 s of silence after mic_on without a corresponding mic_off, it auto-closes the utterance (sends `mic_off` to the app to flip status, then proceeds with encode + send). Prevents stuck-open mic if the user forgets the second click.
- While mic is on, stream `audioEvent.audioPcm` chunks (Uint8Array) **as binary WebSocket frames** — call `ws.send(audioPcm)` directly with the Uint8Array, no base64, no JSON wrapper. Binary WS confirmed working on physical G2 by the reference implementation `nickustinov/stt-even-g2`, which sends PCM frames the same way to a remote STT WebSocket. No buffering on the app side beyond what the SDK delivers.
- On reply frames from the bridge, render text via `TextContainerProperty` + `textContainerUpgrade`. Use the **SDK-typedef form** (single object argument):

  ```ts
  await bridge.textContainerUpgrade(new TextContainerUpgrade({
    containerID: 2,
    containerName: 'reply',
    contentOffset: 0,
    contentLength: 2000,
    content: replyText,
  }))
  ```

  Confirmed against `even-realities/evenhub-templates/asr` (official scaffold). The Even Hub guide doc shows a five-positional-argument form which contradicts the SDK typedef — typedef form wins.

  Display limits per Even Hub Display docs: `contentLength` up to 2,000 chars per update; ~400–500 chars fill the screen at default font; practical reference implementations cap visible tail at ~240–480 chars. Long replies paginate; swipe up / swipe down (`SCROLL_TOP_EVENT` / `SCROLL_BOTTOM_EVENT`) = next/prev page. Final page size is tuned on hardware during integration.
- **Debounce display writes at ~120 ms.** The BLE render queue is slow; the official `evenhub-templates/asr` scaffold debounces at 120 ms to prevent backlog. Apply the same in the agent menu and reply renderer.
- **SDK quirk — `CLICK_EVENT = 0` arrives as `undefined`.** Protobuf `fromJson` omits zero-value fields. When `event.sysEvent?.eventType` (or `textEvent`/`listEvent`) is `undefined` AND a non-audio event envelope is present, treat it as a `CLICK_EVENT`. Both `evenhub-templates/asr` and `nickustinov/stt-even-g2` document and handle this. Implementer must too.
- Render a one-line status indicator (top of display) for: `IDLE`, `LISTEN`, `THINK`, `READ` (length of last reply in pages), `OFFLINE` (WS down).
- **Agent selection menu.** On menu intent (gesture = double-press right temple, verify on hardware), send `list_agents` to the bridge, render the returned list as a scrollable selector (one bot per row, showing display name). Tap selects. Send `select_agent` with the chosen Telegram chat_id. Wait for `agent_selected` confirmation, then return to `IDLE` with the new agent active. The active agent's name is shown in the status bar.
- **Single-active-agent model.** All utterances in the current session route to the selected agent until the user opens the menu and picks another. No per-utterance prefix parsing.
- **Persistence via SDK local storage**: `bridge.setLocalStorage('last_selected_agent_id', '<int-as-string>')` and `bridge.getLocalStorage('last_selected_agent_id')`. Values are strings only — serialize ints to decimal strings. On launch, app reads and sends `select_agent` with the persisted id to restore the previous session immediately. If absent (first launch), app opens the menu automatically.
- No transcription on device.

**Out of scope for v1:**

- Image previews on display.
- IMU-driven UI.
- Storing reply history on device.

### Component B — Bridge (`bridge/jc-glasses/`)

**Tech:** Python 3.11+, `websockets` (async WS server), `telethon` (Telegram MTProto client), `pydantic` (frame validation), `pyyaml` (config), `ffmpeg` (PCM→Opus encoding; system binary, called via `subprocess`).
**Layout (target — implementer may adjust):**

```
jc-glasses-bridge/                       # separate repo
  pyproject.toml
  jc_glasses_bridge/
    __init__.py
    server.py          # WS server, frame router, per-connection session state
    encoder.py         # PCM s16le 16 kHz mono → OGG/Opus voice-note via ffmpeg
    agents.py          # Telethon dialog discovery (filter to bots), agent metadata cache
    telegram_client.py # Telethon session, send voice notes, listen for text/voice replies
    config.py          # YAML config loader
    frames.py          # Pydantic frame schemas
    main.py            # entrypoint
  apps/jc-glasses/     # Even Hub TS app bundle (sideloaded)
  config.example.yaml
  systemd/jc-glasses-bridge.service
```

**Responsibilities:**

- Run a WebSocket server (default `wss://`). Authenticate the app via a shared secret in the first frame (HMAC of timestamp + bridge token). Reject otherwise.
- Maintain **per-WebSocket session state**: `current_agent_id` (Telegram chat_id of the selected bot) and `last_utterance_id`. State lives in memory; it is restored on reconnect when the app re-sends `select_agent` from its KV store.
- **Agent discovery.** On `list_agents` request, iterate Telethon `client.iter_dialogs()`, filter to `User` entities with `entity.bot == True`, return a list of `{ id, name, username, last_message_ts }`. Cache for 60 s to avoid hammering Telegram on rapid menu re-opens. Optional `agents.include_pattern` / `agents.exclude_pattern` filters by username regex in config.
- **Agent selection.** On `select_agent`, verify the requested `agent_id` is in the discovered bot set; store it as the session's `current_agent_id`; reply `agent_selected` with the resolved name. On invalid id reply `agent_select_failed`.
- Buffer PCM frames per utterance. End-of-utterance = mic-off frame OR 800 ms of silence (VAD optional in v1; mic-off frame is sufficient).
- **Encode utterance.** PCM s16le 16 kHz mono → OGG container with Opus codec via `ffmpeg -f s16le -ar 16000 -ac 1 -i - -c:a libopus -b:a 24k -f ogg pipe:1`. In-memory; no temp files. Resulting blob ≤ ~50 KB for a 5 s utterance.
- **Forward as Telegram voice note.** Send via Telethon `client.send_file(chat_id, voice_blob, voice_note=True, caption="[sent from even G2]", attributes=[DocumentAttributeAudio(voice=True, duration=<s>)])` to the session's `current_agent_id`. Caption is informational — visible to the operator in their Telegram thread, not parsed by JC. The bridge does **not** transcribe; ASR happens in JC as it does for every other voice message.
- If `current_agent_id` is unset, reply `error { code: "no_agent_selected" }` and do not send anything to Telegram.
- Subscribe (Telethon `events.NewMessage`) to incoming messages on the same chat. When a reply arrives within the response window (default 600 s; configurable), encode the reply as a `reply` frame and push it down the WebSocket. Replies are text — TTS is out of scope (no speaker on G2).
- Heartbeat every 15 s on idle WebSocket so phone OS doesn't kill it.
- Structured logging to stdout (systemd captures). One log line per: `frame_in`, `utterance_start`, `utterance_end`, `voice_encoded`, `agent_selected`, `telegram_sent`, `reply_in`, `reply_pushed`, `error`. JSON format. No PII in logs beyond chat IDs (no audio bytes ever logged).

**Config (`config.yaml`):**

```yaml
bridge:
  bind: "0.0.0.0:8443"
  public_host: "jcglasses.omnisage.org"
  tls:
    # Reuses operator's existing *.omnisage.org wildcard cert on the host.
    # No new certbot job needed for this subdomain.
    cert: /etc/ssl/omnisage.org/fullchain.pem    # adjust to actual wildcard cert path
    key:  /etc/ssl/omnisage.org/privkey.pem
  shared_secret_env: JC_GLASSES_BRIDGE_SECRET

encoder:
  # PCM → OGG/Opus voice note. Bridge does NOT transcribe; JC does.
  ffmpeg_bin: ffmpeg          # absolute path if not on PATH
  opus_bitrate_kbps: 24       # Telegram voice-note conventional bitrate
  max_utterance_seconds: 60   # safety cap; reject longer mic holds

telegram:
  api_id_env: TG_API_ID
  api_hash_env: TG_API_HASH
  session_path: /var/lib/jc-glasses-bridge/operator.session
  # Phone + OTP + 2FA prompted ONLY on first run via `bridge auth` CLI subcommand.
  # Session file is sufficient for all subsequent runs.

agents:
  voice_caption: "[sent from even G2]"  # caption on the Telegram voice note; informational only
  reply_window_seconds: 600
  discovery: telegram_bots          # always; documented for future alternative sources
  discovery_cache_seconds: 60
  include_pattern: null              # optional regex; null = include all bots
  exclude_pattern: '^(BotFather|SpamBot|StickerBot|GIF|imdb|gif|vid|pic|youtube|wiki)$'  # default exclude for non-agent bots; extend as needed
  first_launch_behavior: prompt_menu # 'prompt_menu' = open menu when no last selection;
                                     # 'first_alphabetical' = auto-select first bot
```

**CLI (`bridge` subcommands):**

- `bridge auth` — one-time Telethon login. Prompts for phone, OTP, 2FA. Writes `session_path`.
- `bridge run` — start the WS server. Used by systemd.
- `bridge agents` — print the discovered bot list (id, name, username, last message ts). Sanity check after auth.
- `bridge test-send <agent_id> <wav_path>` — encode the given WAV (16 kHz mono) to an Opus voice note and forward to the given Telegram bot as if from the glasses; observe reply. Useful for end-to-end testing without glasses.

## Wire protocol — Bridge ⇄ Even Hub app

WebSocket. The first frame after connect is an **auth** frame; the server rejects the connection if absent or invalid.

All control frames are JSON text. Audio is binary.

### Frames from app → bridge

| Type | Encoding | Schema | When |
|---|---|---|---|
| `auth` | JSON text | `{ "type": "auth", "ts": <unix>, "hmac": "<hex>", "app_version": "..." }` | First frame after connect. `hmac` = `HMAC-SHA256(secret, str(ts))`. Reject if `\|now - ts\| > 60 s`. |
| `list_agents` | JSON text | `{ "type": "list_agents" }` | App opens the agent menu. Bridge replies with `agent_list`. |
| `select_agent` | JSON text | `{ "type": "select_agent", "agent_id": <int> }` | App picks an agent. `agent_id` is the Telegram chat_id from `agent_list`. Bridge replies `agent_selected` or `agent_select_failed`. App may also send this on connect to restore the persisted selection. |
| `mic_on` | JSON text | `{ "type": "mic_on", "utterance_id": "<uuid>" }` | First `CLICK_EVENT` on left temple — start of utterance. |
| `audio` | binary | raw PCM s16le 16 kHz mono, chunks as delivered by SDK | While mic is on. |
| `mic_off` | JSON text | `{ "type": "mic_off", "utterance_id": "<uuid>" }` | Second `CLICK_EVENT` on left temple — end of utterance. May also be sent by the bridge to the app if its 3 s silence safety net fires. |
| `ack` | JSON text | `{ "type": "ack", "reply_id": "<uuid>" }` | App acknowledges a `reply` frame (so bridge can drop it from retry buffer). |
| `cancel` | JSON text | `{ "type": "cancel", "utterance_id": "<uuid>" }` | App cancels a pending utterance (touch dismiss before reply arrives). Bridge stops waiting for that reply. |
| `pong` | JSON text | `{ "type": "pong" }` | Reply to bridge ping. |

### Frames from bridge → app

| Type | Encoding | Schema | When |
|---|---|---|---|
| `auth_ok` | JSON text | `{ "type": "auth_ok" }` | Auth accepted. |
| `auth_fail` | JSON text | `{ "type": "auth_fail", "reason": "..." }` | Then close. |
| `agent_list` | JSON text | `{ "type": "agent_list", "agents": [{ "id": <int>, "name": "Harold Finch", "username": "harold_finch_bot", "last_message_ts": <int>\|null }, ...] }` | Reply to `list_agents`. Sorted by `last_message_ts` desc (most recently used first), with never-messaged bots last alphabetically. |
| `agent_selected` | JSON text | `{ "type": "agent_selected", "agent_id": <int>, "name": "Harold Finch" }` | Confirms `select_agent`. App updates status bar with the agent name. |
| `agent_select_failed` | JSON text | `{ "type": "agent_select_failed", "agent_id": <int>, "reason": "not_a_bot"\|"not_found"\|"excluded_by_filter" }` | Selection rejected. App reopens the menu. |
| `status` | JSON text | `{ "type": "status", "phase": "encoding"\|"sent"\|"waiting", "utterance_id": "<uuid>" }` | Progress signals so the app can show `LISTEN → THINK → READ`. `encoding` = PCM→Opus on the bridge; `sent` = voice note dispatched to Telegram; `waiting` = waiting for the agent's reply. |
| `reply` | JSON text | `{ "type": "reply", "reply_id": "<uuid>", "utterance_id": "<uuid>"\|null, "agent_id": <int>, "agent_name": "Harold Finch", "text": "...", "pages": <int> }` | Agent answer arrived; render. `utterance_id` may be null if the reply is unsolicited (agent-initiated). |
| `error` | JSON text | `{ "type": "error", "utterance_id": "<uuid>"\|null, "code": "no_agent_selected"\|"encode_failed"\|"telegram_send_failed"\|"reply_window_expired"\|..., "message": "..." }` | Errors. App may auto-open the menu on `no_agent_selected`. |
| `ping` | JSON text | `{ "type": "ping" }` | Every 15 s on idle. |

### Reconnection

- On WS drop, app retries with exponential backoff (1 s, 2 s, 4 s, 8 s, capped at 30 s).
- The bridge maintains a small retry buffer of unacknowledged `reply` frames keyed by `reply_id`. On reconnect (same auth identity), it replays unacked replies.

## ASR — handled by JC, not the bridge

**The bridge does not transcribe.** It encodes the buffered PCM into an OGG/Opus voice note and forwards it via Telethon to the selected agent's Telegram bot DM. The existing JC voice channel (`lib/voice/asr.py`, Dashscope `qwen2.5-omni-7b`) handles ASR, triage, and reply generation.

### Why

- Operator decision 2026-06-02: keep ASR on the JC side. Single source of truth for transcription, no key on the bridge, no drift, voice notes archived in Telegram.
- Measured ASR latency on Luca's host (2026-06-02 benchmark): Dashscope `qwen2.5-omni-7b` ~1.5 s, OpenAI `whisper-1` ~2.5 s. Both multilingual. Bridge-side ASR offered no latency or accuracy benefit.
- The "15–30 s" Dashscope figure earlier in this spec referred to the full voice pipeline (ASR + triage + LLM + TTS), not ASR alone.

### No JC changes required

JC voice channel receives the voice note as a standard Telegram voice message and processes it identically to any other voice input. The caption `[sent from even G2]` is visible in the operator's Telegram thread for audit/searchability but is not parsed by JC.

Per-persona `[glasses]` formatting rule (see "Source tagging" → "Per-persona instruction") is a future STYLE.md / RULES.md addition per instance — also requires no JC framework changes.

### Out of scope on the bridge

- ASR (any flavor).
- TTS (no speaker on G2).
- Triage / model routing.

## Agent selection — menu-driven, single-active

The bridge does **not** parse the transcript for an agent name. The active agent is a per-session state set explicitly by the operator from a menu on the glasses.

### Discovery — list source is Telegram

The "agents" the operator can pick from = the **Telegram bots in the operator's own dialog list**. Implementation in Telethon:

```python
async for dialog in client.iter_dialogs():
    entity = dialog.entity
    if isinstance(entity, User) and entity.bot:
        yield {
          "id": entity.id,                 # Telegram chat_id
          "name": entity.first_name or entity.username,
          "username": entity.username,
          "last_message_ts": int(dialog.date.timestamp()) if dialog.date else None,
        }
```

Filter further with optional `agents.include_pattern` / `agents.exclude_pattern` regexes on the username (e.g., to exclude `BotFather`, `SpamBot`, etc.).

The list is **always live**. Adding a new agent = the operator starts a new chat with that bot in Telegram. It appears in the menu on next `list_agents`.

### Selection model

- **Single active agent per session.** The operator picks once; every subsequent utterance goes to that agent until they pick another.
- **Trigger to open the menu:** dedicated gesture on the glasses (double-press right temple — verify and adjust during implementation). The app sends `list_agents`, renders the returned `agent_list`, lets the operator scroll-and-tap to pick one, sends `select_agent`, waits for `agent_selected`, returns to `IDLE`.
- **Persistence across launches.** App stores the last `agent_id` in the SDK Key-Value Store. On launch + auth, it sends `select_agent` with that id so the previous session resumes immediately. If no persisted id and `agents.first_launch_behavior: prompt_menu` (default), the app auto-opens the menu. If `first_alphabetical`, the app auto-selects the alphabetically first bot from the discovered list.

### What the status bar shows

`<agent name> · <phase>` — e.g., `Harold · IDLE`, `Rachel · THINK`, `Harold · READ 2/4`. Makes the active agent always visible, no ambiguity about who the operator is talking to.

### Negative cases — explicit errors back to glasses

- `select_agent` for an id that is not a bot or not in the operator's dialogs → `agent_select_failed { reason: "not_a_bot" \| "not_found" }`. App reopens the menu.
- Utterance arrives with no `current_agent_id` set on the session → `error { code: "no_agent_selected" }`. App auto-opens the menu.
- `list_agents` returns an empty list (no bots in the operator's Telegram) → `agent_list { agents: [] }`. App shows "No agents available — DM a bot in Telegram first".

### Why no broadcast in v1

Confirmed v1 = single-active-agent. Concurrent multi-agent ("tell everyone") is deferred until single-agent flow is validated on real hardware. When added, it becomes a special menu entry "All agents" that broadcasts and renders replies as a stacked card stream.

## Source tagging

The bridge attaches `[sent from even G2]` as a **Telegram caption** on the voice note. This is an informational label — the operator sees it in their Telegram thread for searchability and audit. JC does not parse it; JC's voice channel receives a standard voice message and processes it as-is.

### Rationale

- JC triage and model routing remain untouched. A glasses voice note routes through triage → selects model (Haiku/Sonnet/Opus) based on content complexity identically to a voice note sent from the Telegram app. If the operator asks a heavy analysis question via glasses, Opus fires.
- Zero JC changes required to ship v1.
- The caption is visible in the operator's Telegram thread. This replaces the earlier "source tag prepended to transcript" design.

### Out of scope for this spec

- Per-persona formatting rules keyed on the caption. Operator decision 2026-06-02: no per-persona rule. The voice note arrives as a normal voice message; the persona answers in its normal voice. Display pagination handles long replies.

## Notification model — replacing "OS push"

The G2 SDK has no documented OS-level push API to wake a closed app. The bridge does not need one because:

1. The app holds an open WebSocket while foregrounded.
2. The operator's interaction pattern is: glance at glasses, speak, wait, read. The app is foregrounded during the wait. There is no "answer arrives an hour later while phone is in pocket" requirement.
3. If the WebSocket drops mid-wait, the bridge keeps the agent reply in its retry buffer (`reply_id`-keyed) for `reply_window_seconds`. On reconnect, the unacked reply is pushed immediately and the operator sees it as soon as the app reopens.

### Recovery flow — pulling buffered replies after the app was closed

Operator-driven recovery, no OS push required. Two redundant paths, either works.

**Path 1 — from the glasses (preferred for hands-free operation):**

1. Operator gestures on the temple to bring up the Even Hub menu on the display.
2. Selects the JC Glasses app entry.
3. Even Hub foregrounds the app on the phone.
4. App reconnects to the bridge WebSocket on launch (existing retry logic).
5. Bridge replays every unacked `reply` frame in the retry buffer for that auth identity.
6. Replies render on the display in original arrival order. App emits `ack` per reply; bridge drops them from the buffer.

Elapsed gesture-to-render: 2–4 s in the good case (single double-press resumes last-used app, reply already on screen by the time the operator looks up), 4–8 s in the worst case (navigate the menu).

**Path 2 — from the phone:**

Operator opens the Even Hub app manually on the phone. Same reconnect + buffer-flush as Path 1. Used when the glasses are off or out of reach.

**Gesture count depends on Even Hub's "resume last app" behavior — verify on physical G2 during implementation:**

- If supported (likely): one double-press on the temple → app foregrounds → reply renders.
- If not: temple gesture → scroll menu → click. Three gestures.

Either way, no data loss while the operator was away. The retry buffer holds replies for `reply_window_seconds` (default 600 s, configurable). Replies older than the window are discarded with an `error { code: "reply_window_expired" }` posted on reconnect so the operator knows something was dropped.

### Open decision (see below)

If real OS-level push becomes a requirement, two paths exist:
(a) Push via the operator's phone OS using Even Hub's eventual background-app facilities (not currently documented).
(b) Mirror unacked replies to a separate Telegram chat the operator already gets push notifications from. Worst case but always works.

## Security

- **Telegram session.** Stored at `telegram.session_path`. File mode `0600`, owner = bridge service user. Not in the repo, not in backups that leave the host. `bridge auth` is the only way to (re)generate it; OTP + 2FA prompted on stdin. RULES §1 trust level T3 — explicit operator action.
- **Shared secret (`JC_GLASSES_BRIDGE_SECRET`).** 32 bytes, generated at install time, stored in the host env (systemd `EnvironmentFile`). Same secret embedded in the Even Hub app bundle at build time. Rotation = regenerate + rebuild app bundle + push update.
- **TLS.** WSS only. Self-signed not permitted (WebView CORS + cert pinning behavior on iOS/Android makes self-signed brittle). Use Let's Encrypt.
- **Domain whitelist.** Bridge's domain is the only entry in the Even Hub app's `app.json` network whitelist. No other outbound destinations from the app.
- **No ASR keys on the bridge.** Bridge does not transcribe — no `DASHSCOPE_API_KEY`, no `OPENAI_API_KEY`. ASR happens inside JC, which already holds its own Dashscope key.
- **No third-party services in the audio path.** Voice notes travel: bridge → operator's own Telegram account → JC's own Telegram bot → JC's own ASR. End-to-end inside infrastructure the operator already controls.
- **No audio recording on disk** by default. PCM frames are held only in the per-utterance in-memory buffer. The WAV blob handed to ASR is discarded after the response. `debug_record_utterances: true` writes WAVs to a configurable path only when the operator explicitly enables it for troubleshooting.
- **Bridge auth identity.** The shared-secret auth is sufficient because the entire chain is operator-only (one app, one bridge, one Telegram account). The bridge is **not** a multi-tenant service.

## Operational

- **Systemd unit** at `bridge/jc-glasses/systemd/jc-glasses-bridge.service`. `Restart=on-failure`, `RestartSec=5`. `EnvironmentFile=/etc/jc-glasses-bridge/env`.
- **Host:** **decided — same host as the JC gateways.** Single box with a public IPv4. No additional infrastructure to provision. Bridge is a peer process to the gateways; if the host goes down, all paths go down together, which is the existing failure mode.
- **DNS + TLS.** Subdomain `jcglasses.omnisage.org` → A record → public IP of the JC host. Reuses the operator's existing `*.omnisage.org` wildcard TLS cert already on the host — no new certbot job. DNS task: one A record in the `omnisage.org` zone, one-time. Cert renewal is owned by whatever already maintains the wildcard cert.
- **Inbound port.** WSS on TCP 8443 (configurable). Firewall opens 8443/tcp inbound on the public IP. ACME (port 80) needed during cert issuance/renewal — close after if undesired.
- **Health:** `bridge run` exposes a small HTTP `/healthz` on localhost (separate port from the WSS) that returns `200` if Telethon is connected and the WS server is accepting. The existing `jc watchdog` can be extended later to watch this.
- **Observability:** structured JSON logs to stdout → journald. Operator runs `journalctl -u jc-glasses-bridge -f` to tail.

## Testing

- **Unit:** `frames.py` schema round-trips, agent discovery filter (mocked Telethon dialogs with mix of bots / users / channels), include/exclude regex, session state transitions, PCM→Opus encoding (verify output is a valid OGG container Telegram will accept as a voice note).
- **Integration (no glasses):** `bridge test-send <agent_id> <wav_path>` encodes the canned WAV to Opus, sends it as a Telegram voice note via Telethon, and listens for the agent's reply. CI-friendly with a mock Telethon client.
- **Simulator:** Even Hub `even-dev` simulator (`BxNxM/even-dev`) runs the Even Hub app bundle against a mock G2. Replay a canned PCM file as `audio` frames. Validates the full menu → select → speak → reply loop without physical hardware.
- **Real glasses (manual sign-off):**
   - First-launch with no persisted agent → menu opens automatically → pick Harold → speak → reply renders.
   - Quit and relaunch → previous Harold selection restored → speak → reply.
   - Open menu (double-press right temple) → switch to Rachel → speak → Rachel replies.
   - Drop WiFi mid-reply → reconnect → reply arrives via retry buffer.
   - 90-second reply (force the active agent to delay) → app stays on `THINK` → reply renders when ready.
   - Send utterance with no agent ever selected (unlikely via UI, force via WS test) → `error no_agent_selected` → app opens menu.

## Build estimate (refresher)

| Component | Estimate |
|---|---|
| Bridge (Python: WS server + ffmpeg PCM→Opus + Telethon voice-note send + session state) | 1–1.5 days |
| Even Hub app (TS + SDK: mic, WS, display, paginate, **agent menu UI**) | 2–2.5 days |
| Agent discovery (Telethon bot filter + cache) | 0.5 day |
| Integration on simulator + real glasses | 1 day |
| Hardening (auth, TLS via certbot, systemd, logs, retry buffer) | 0.5–1 day |
| **Total** | **5–6.5 days** |

## Decisions log + open items

### Resolved (operator sign-off 2026-06-02)

1. **Hosting.** Single host, same box as the JC gateways. Public IPv4 on that host.
2. **TLS / cert.** `jcglasses.omnisage.org` reuses the operator's existing `*.omnisage.org` wildcard cert already on the host. No new certbot job. WSS-only. A record points at the public IP of the JC host. DNS provider holds the `omnisage.org` zone.
3. **Agent model.** Single-active-agent per session, menu-driven selection from the operator's Telegram bot list (Telethon `iter_dialogs` filtered to `User.bot==True`). **No prefix parsing.** No hardcoded "default agent" — the persisted last-selected agent is what restores on launch.
4. **"Tell everyone" broadcast.** **Deferred** past v1. Add later as a special menu entry once single-agent flow is validated on real hardware.
5. **Continuity within a mic session.** Superseded by menu-driven selection — irrelevant now.
6. **Background OS push.** **Not required.** Foreground-only model accepted. Recovery flow (`Notification model → Recovery flow`) covers reopening the app from the glasses.
7. **App distribution.** Sideload only. The Even Hub app has the bridge URL and shared secret baked in at build time; nothing useful in publishing to the public store.

### Also resolved 2026-06-02

8. **Subdomain.** `jcglasses.omnisage.org`. Operator owns the zone. Wildcard `*.omnisage.org` TLS cert already on the host — no new cert provisioning needed.
9. **First-launch behavior.** `prompt_menu` (open the agent menu on first launch). After first pick, persist `last_selected_agent_id` in the SDK KV store; every subsequent launch restores that agent automatically. Operator switches agents at any time via the menu gesture.
10. **Default bot exclude regex.** Enabled by default to keep the menu clean:
    ```yaml
    exclude_pattern: '^(BotFather|SpamBot|StickerBot|GIF|imdb|gif|vid|pic|youtube|wiki)$'
    ```
    Operator may extend.
11. **ASR location = JC, zero JC changes.** Bridge forwards audio as a Telegram voice note (caption `[sent from even G2]`) via Telethon. JC's existing voice channel receives a standard voice message — no new code needed. Benchmarked live 2026-06-02: Dashscope `qwen2.5-omni-7b` ~1.5 s, OpenAI Whisper ~2.5 s, both multilingual. Voice notes archived in Telegram thread.
12. **Repo split.** Bridge ships in a **separate repo** (`jc-glasses-bridge`) co-locating the Python bridge and the Even Hub TS app. JC monorepo holds the spec (this file) only. Reasoning: bridge is transport for one input device, not part of agent runtime; TS app doesn't fit Python stack; independent release cadence; zero JC code touched.
13. **Mic trigger = single `CLICK_EVENT` on left temple, toggle semantics.** First click starts, second click ends the utterance. No press-and-hold (not exposed as an SDK event — verified against Even Hub `Input & Events` docs which expose only `CLICK_EVENT`, `DOUBLE_CLICK_EVENT`, `SCROLL_TOP_EVENT`, `SCROLL_BOTTOM_EVENT`). Bridge runs a 3 s silence safety net to auto-close the utterance if the user forgets the second click.
14. **Subdomain = `jcglasses.omnisage.org`, reuse existing wildcard cert.** No new certbot job. Wildcard `*.omnisage.org` cert is already provisioned on the host. Cert/key paths in config are placeholders; operator points them at the actual wildcard cert files.
15. **Display capacity claim corrected.** `textContainerUpgrade` accepts max 2,000 chars; ~400–500 chars fill a full-screen container at default font (Even Hub Display docs). Spec previously said "~3 lines × ~50 chars" — unverifiable and likely under-reporting capacity. Implementer tunes page size on hardware.
16. **No per-persona formatting rule.** Voice notes arrive at the persona as normal voice input — no caption-based formatting tweak, no `[glasses]` prefix rule. Long replies paginate on the display.
17. **Goal section rewritten** to reflect the resolved architecture (bridge does not transcribe, JC voice channel handles ASR + triage + brain unchanged).
18. **Binary WebSocket frames confirmed (item 1).** Reference impl `nickustinov/stt-even-g2` sends `event.audioEvent.audioPcm` (Uint8Array) over `ws.send(...)` directly to a remote STT WebSocket on physical G2 hardware. Binary WS path works. Spec stays on binary frames as primary; no base64-JSON fallback in v1. If a future G2 firmware ever breaks this, add the fallback then.
19. **`textContainerUpgrade` signature decided (item 7).** Use the SDK typedef form: `bridge.textContainerUpgrade(new TextContainerUpgrade({ containerID, containerName, contentOffset, contentLength, content }))`. Confirmed by the official `even-realities/evenhub-templates/asr` scaffold. The Even Hub guide doc's five-positional-argument form is wrong or stale and is ignored.
20. **SDK quirks adopted into spec.** (a) Protobuf normalizes `CLICK_EVENT = 0` to `undefined` on `fromJson`; the app must treat undefined-event-type with a non-audio event envelope as a click. (b) BLE render queue is slow; debounce display writes at ~120 ms. Both confirmed by official asr template and stt-even-g2 reference impl.

### Still open / non-blocking

D. **Menu gesture.** Determined during implementation by trial on physical G2. Spec proposal (right-temple double-press = `DOUBLE_CLICK_EVENT` with `eventSource = TOUCH_EVENT_FROM_GLASSES_R`) is provisional. Implementer picks the gesture that doesn't conflict with Even Hub OS reservations.

## Out of scope (future work, not blocking v1)

- TTS to operator via phone speaker while glasses display reply (would re-introduce `lib/voice/synth.py` into this path).
- Image deliverables on the glasses display (infographic preview).
- Reply history browser on the glasses (scroll back through last N exchanges).
- Local ASR via whisper.cpp on the bridge for full-offline path.
- Bridge as a multi-tenant service for multiple operators.
- Group-chat routing (current spec is 1:1 DMs only).

## References

- Even Realities developer docs: https://hub.evenrealities.com/docs
- Even Hub SDK: https://www.npmjs.com/package/@evenrealities/even_hub_sdk
- **Official Even Hub starter templates (canonical scaffolds): https://github.com/even-realities/evenhub-templates** — `asr/` template is the closest reference. Confirms `textContainerUpgrade(new TextContainerUpgrade({...}))` form, 120 ms BLE render debounce, `CLICK_EVENT=0`-as-undefined quirk handling, and the recommended event-routing pattern.
- **Reference STT implementation on G2 hardware: https://github.com/nickustinov/stt-even-g2** — production-ish bridge sending `audioPcm` Uint8Array as **binary WebSocket frames** to Soniox. Direct confirmation that binary WS works on physical G2. Also uses single-click toggle for mic, matching this spec's decision #13.
- Even Hub simulator: https://github.com/BxNxM/even-dev
- Even Demo App (G1, useful for BLE protocol reference): https://github.com/even-realities/EvenDemoApp
- Telethon docs: https://docs.telethon.dev
- Telethon `send_file` with voice notes: https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.uploads.UploadMethods.send_file
- Dashscope `qwen2.5-omni-7b` multimodal generation (used inside JC, not bridge): https://help.aliyun.com/zh/dashscope/developer-reference/qwen-omni-api
- Existing JC voice ASR: `lib/voice/asr.py`
- Existing JC gateway config schema: `ops/gateway.yaml`, `docs/specs/unified-gateway-0.3.0-remaining.md`
- OpenClaw bridge (explicitly NOT used, kept here as the rejected alternative): https://github.com/dAAAb/openclaw-even-g2-bridge-skill
