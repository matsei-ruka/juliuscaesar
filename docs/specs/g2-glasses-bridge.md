# Spec: Even Realities G2 Glasses ↔ JC Bridge

**Status:** Draft
**Date:** 2026-06-01
**Branch base:** `main`
**Branch:** `spec/g2-glasses-bridge`
**Owner:** TBD

## Goal

Let a Luca-class operator talk to **any JC agent** (Harold, Rachel, future personas) from Even Realities G2 smart glasses, by voice, with no arbitrary latency cutoff and without handing third-party apps the operator's Telegram credentials.

The glasses become a thin client to the operator's existing JC agent fleet:

1. Operator speaks into the G2 mic, optionally prefixed with an agent name ("tell Harold ...", "Rachel, check ...").
2. Audio streams as raw PCM to a self-hosted Python bridge.
3. Bridge transcribes server-side and routes the message to the addressed agent's Telegram conversation, **as the operator's own Telegram user account**, using Telethon (MTProto user API — not bot API).
4. Agent replies in its usual conversation; the bridge mirrors the reply back to the glasses display over the same WebSocket and renders it.
5. No 20-second deadline. If the agent takes 90s, the glasses display the answer when it arrives.

The bridge is the only new piece. The existing JC gateway, persona instances, voice pipeline, and Telegram conversations are unchanged.

## Non-goals

- Do not reuse, repackage, or fork the OpenClaw Even-G2 bridge (`dAAAb/openclaw-even-g2-bridge-skill`). Its 22-second hard deadline and single-agent routing model are explicit anti-requirements here.
- Do not use the G2 stock "Even AI" app or its OpenAI-compatible chat-completions configuration. We build our own Even Hub app so we control mic capture, display rendering, and the wire protocol end to end.
- Do not use a Telegram **bot** account for routing. Operator wants the glasses to speak as *himself* into the existing 1:1 agent conversations (chat 28547271 ↔ Harold, etc.), not as a third party. This is non-negotiable.
- Do not transcribe on the glasses. Server-side ASR only. Glasses send raw PCM; the bridge owns transcription quality.
- Do not modify the JC gateway, persona brains, or Telegram channel adapter for this feature. The bridge is fully external to JC — it talks to JC the same way a phone does.
- Do not introduce a new agent-addressing syntax inside the JC framework. The bridge resolves agent name → Telegram chat ID locally and sends to that chat. JC sees normal Telegram messages.
- Do not require Docker. Native Python + a single Even Hub app bundle. systemd unit for the bridge.

## Background

### G2 hardware + SDK (verified 2026-06-01)

Source: `hub.evenrealities.com/docs`, `@evenrealities/even_hub_sdk` (npm), and SDK feature-verification reports from third-party developers.

| Capability | Status | Detail |
|---|---|---|
| Raw mic audio | **Confirmed** | `audioControl(true)` enables mic. PCM streams via `audioEvent.audioPcm` as `Uint8Array`. Format: **16 kHz mono 16-bit signed little-endian**. Requires `g2-microphone` permission in `app.json`. |
| Text on display | **Confirmed** | `TextContainerProperty` + `textContainerUpgrade` render text with flicker-free partial updates. Display: 576×288 px per eye, 4-bit grayscale. Practical text capacity ≈ 3 lines × ~50 chars. |
| Touch input | **Confirmed** | Click, double-click, scroll on left/right temple + R1 ring. Use for scroll-through-paginated-reply and dismiss. |
| Outbound networking | **Confirmed** | `fetch()`, `XMLHttpRequest`, WebSocket from the app WebView. Two gates: (a) domains whitelisted in `app.json`, (b) standard CORS from server side. Binary frames over WebSocket are supported. |
| OS-level push notification (app closed) | **Not in SDK** | No documented push API to wake the app or post to a system notification tray. **Worked around** by holding a persistent WebSocket from the app to the bridge while the app is foregrounded; for background, see "Open decisions" below. |
| Speaker | **Absent** | G2 has no speaker. TTS does not apply to this path. Display-only output. |
| Image on display | **Confirmed** | 1-bit / 4-bit grayscale images via container API. Not required for v1 but useful later (e.g., infographic preview). |

A simulator exists: `BxNxM/even-dev` (Even Realities Hub Simulator — multi-app test environment). Development and CI tests can run against the simulator before deploying the bundle to physical G2.

### JC current state (relevant pieces)

- `lib/voice/asr.py` — Dashscope `qwen2.5-omni-7b` REST transcription. Heavy (15–30 s per request). Used by the existing Telegram voice path.
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
+----------------+     BLE      +-------------+   WiFi/4G    +------------------+
|     G2 mic     | ===========> | Even Hub    | ===========> |  Bridge (Python) |
|  audioPcm 16k  |              |  custom app |   WebSocket  |  - WS server     |
+----------------+              |  (TS/HTML)  | <=========== |  - ASR (Whisper) |
                                |             |   text+meta  |  - Router        |
+----------------+              |             |              |  - Telethon      |
|  G2 display    | <=========== |             |              +---------+--------+
|  text/scroll   |              +-------------+                        |
+----------------+                                                     | Telegram MTProto
                                                                       | (as operator user)
                                                                       v
                                                       +----------------------------------+
                                                       | Telegram (operator's account)    |
                                                       |  • DM with Harold (chat 28547271)|
                                                       |  • DM with Rachel (chat ...)     |
                                                       |  • DM with <future agents>       |
                                                       +-----------------+----------------+
                                                                         |
                                                                         v
                                                       Existing JC gateways receive the
                                                       message, triage, brain, reply as
                                                       today. No JC changes.
```

### Three actors

1. **Even Hub app** — small custom TypeScript/HTML bundle running in the Even Hub WebView on the operator's phone. Captures mic, streams PCM frames over WebSocket, receives reply frames, renders text.
2. **Bridge** — Python daemon on the operator's server. Speaks WebSocket south (to the app) and Telethon-MTProto north (to Telegram). Runs the ASR and the agent router.
3. **JC fleet** — unchanged. Each persona instance reads the Telegram message addressed to its conversation and answers as it always does. The bridge then mirrors that answer back to the glasses.

## Components

### Component A — Even Hub app (`apps/jc-glasses/`)

**Tech:** TypeScript + Even Hub SDK + minimal HTML.
**Manifest (`app.json`):** declare permissions `g2-microphone`, networking whitelist for the bridge's WSS domain.
**Responsibilities:**

- Open and hold a single WebSocket to the bridge. Reconnect with exponential backoff on drop.
- On mic-on intent (touch event = press-and-hold left temple, see "Input model" below), call `audioControl(true)`. On mic-off, call `audioControl(false)`.
- While mic is on, stream `audioEvent.audioPcm` chunks (Uint8Array) as binary WebSocket frames. No buffering on the app side beyond what the SDK delivers.
- On reply frames from the bridge, render text via `TextContainerProperty` + `textContainerUpgrade`. Long replies paginate to fit ~3 lines × 50 chars. Touch swipe = next page.
- Render a one-line status indicator (top of display) for: `IDLE`, `LISTEN`, `THINK`, `READ` (length of last reply in pages), `OFFLINE` (WS down).
- No transcription on device. No persistence beyond the SDK Key-Value Store for: bridge URL, last-used agent name (for "no name, send to last" behavior).

**Out of scope for v1:**

- Image previews on display.
- IMU-driven UI.
- Storing reply history on device.

### Component B — Bridge (`bridge/jc-glasses/`)

**Tech:** Python 3.11+, `websockets` (async WS server), `telethon` (Telegram MTProto client), `httpx` (ASR HTTP), `pydantic` (frame validation), `pyyaml` (config).
**Layout (target — implementer may adjust):**

```
bridge/jc-glasses/
  pyproject.toml
  jc_glasses_bridge/
    __init__.py
    server.py          # WS server, frame router
    asr.py             # Whisper (primary) + Dashscope (fallback) adapters
    router.py          # parse agent name, lookup chat ID
    telegram_client.py # Telethon session, send/listen
    config.py          # YAML config loader
    frames.py          # Pydantic frame schemas
    main.py            # entrypoint
  config.example.yaml
  systemd/jc-glasses-bridge.service
```

**Responsibilities:**

- Run a WebSocket server (default `wss://`). Authenticate the app via a shared secret in the first frame (HMAC of timestamp + bridge token). Reject otherwise.
- Buffer PCM frames per utterance. End-of-utterance = mic-off frame OR 800 ms of silence (VAD optional in v1; mic-off frame is sufficient).
- Submit the utterance audio to the ASR adapter. Return transcript + confidence.
- Pass transcript to the router. Router parses optional agent prefix and resolves to a Telegram chat ID via the agent registry. If no agent name is found, route to `default_agent` from config.
- Build the outbound message: `[glasses] <transcript>` (the source tag is literal; see "Source tagging" below) and send via Telethon to the resolved chat.
- Subscribe (Telethon `events.NewMessage`) to incoming messages on the same chat. When a reply arrives within the response window (default 600 s; configurable), encode the reply as a `reply` frame and push it down the WebSocket.
- Heartbeat every 15 s on idle WebSocket so phone OS doesn't kill it.
- Structured logging to stdout (systemd captures). One log line per: `frame_in`, `utterance_start`, `utterance_end`, `asr_done`, `route_decided`, `telegram_sent`, `reply_in`, `reply_pushed`, `error`. JSON format. No PII in logs beyond chat IDs (no transcript bodies unless `debug_log_transcripts: true`).

**Config (`config.yaml`):**

```yaml
bridge:
  bind: "0.0.0.0:8443"
  tls:
    cert: /etc/letsencrypt/live/<host>/fullchain.pem
    key: /etc/letsencrypt/live/<host>/privkey.pem
  shared_secret_env: JC_GLASSES_BRIDGE_SECRET

asr:
  primary: whisper      # whisper | dashscope
  whisper:
    api_key_env: OPENAI_API_KEY
    model: whisper-1
    language: null       # auto-detect; or "en", "it"
  dashscope:             # fallback if whisper fails or times out
    api_key_env: DASHSCOPE_API_KEY
    model: qwen2.5-omni-7b
    timeout_seconds: 30
  fallback_after_seconds: 8

telegram:
  api_id_env: TG_API_ID
  api_hash_env: TG_API_HASH
  session_path: /var/lib/jc-glasses-bridge/operator.session
  # Phone + OTP + 2FA prompted ONLY on first run via `bridge auth` CLI subcommand.
  # Session file is sufficient for all subsequent runs.

router:
  default_agent: harold
  source_tag: "[glasses]"
  reply_window_seconds: 600
  agents:
    harold:
      chat_id: 28547271
      aliases: ["harold", "finch"]
    rachel:
      chat_id: <RACHEL_CHAT_ID>   # filled at install time
      aliases: ["rachel", "zane"]
    # future agents append here
```

**CLI (`bridge` subcommands):**

- `bridge auth` — one-time Telethon login. Prompts for phone, OTP, 2FA. Writes `session_path`.
- `bridge run` — start the WS server. Used by systemd.
- `bridge agents` — list configured agents and their resolved chat IDs (sanity check).
- `bridge test-send <agent> <text>` — send a synthetic message as if from the glasses, observe reply (useful for end-to-end testing without glasses).

## Wire protocol — Bridge ⇄ Even Hub app

WebSocket. The first frame after connect is an **auth** frame; the server rejects the connection if absent or invalid.

All control frames are JSON text. Audio is binary.

### Frames from app → bridge

| Type | Encoding | Schema | When |
|---|---|---|---|
| `auth` | JSON text | `{ "type": "auth", "ts": <unix>, "hmac": "<hex>", "app_version": "..." }` | First frame after connect. `hmac` = `HMAC-SHA256(secret, str(ts))`. Reject if `\|now - ts\| > 60 s`. |
| `mic_on` | JSON text | `{ "type": "mic_on", "utterance_id": "<uuid>" }` | Press-and-hold start. |
| `audio` | binary | raw PCM s16le 16 kHz mono, chunks as delivered by SDK | While mic is on. |
| `mic_off` | JSON text | `{ "type": "mic_off", "utterance_id": "<uuid>" }` | Press release. End of utterance. |
| `ack` | JSON text | `{ "type": "ack", "reply_id": "<uuid>" }` | App acknowledges a `reply` frame (so bridge can drop it from retry buffer). |
| `cancel` | JSON text | `{ "type": "cancel", "utterance_id": "<uuid>" }` | App cancels a pending utterance (touch dismiss before reply arrives). Bridge stops waiting for that reply. |
| `pong` | JSON text | `{ "type": "pong" }` | Reply to bridge ping. |

### Frames from bridge → app

| Type | Encoding | Schema | When |
|---|---|---|---|
| `auth_ok` | JSON text | `{ "type": "auth_ok" }` | Auth accepted. |
| `auth_fail` | JSON text | `{ "type": "auth_fail", "reason": "..." }` | Then close. |
| `status` | JSON text | `{ "type": "status", "phase": "asr"\|"routing"\|"sent"\|"waiting", "utterance_id": "<uuid>" }` | Progress signals so the app can show `LISTEN → THINK → READ`. |
| `transcript` | JSON text | `{ "type": "transcript", "utterance_id": "<uuid>", "text": "...", "agent": "harold" }` | After ASR + routing; lets the app show "to Harold: ..." for ~1 s. |
| `reply` | JSON text | `{ "type": "reply", "reply_id": "<uuid>", "utterance_id": "<uuid>"\|null, "agent": "harold", "text": "...", "pages": <int> }` | Agent answer arrived; render. `utterance_id` may be null if the reply is unsolicited (agent-initiated). |
| `error` | JSON text | `{ "type": "error", "utterance_id": "<uuid>"\|null, "code": "...", "message": "..." }` | ASR failure, no agent matched, Telegram send failed, reply window timed out. |
| `ping` | JSON text | `{ "type": "ping" }` | Every 15 s on idle. |

### Reconnection

- On WS drop, app retries with exponential backoff (1 s, 2 s, 4 s, 8 s, capped at 30 s).
- The bridge maintains a small retry buffer of unacknowledged `reply` frames keyed by `reply_id`. On reconnect (same auth identity), it replays unacked replies.

## Server-side ASR

**Primary: OpenAI `whisper-1`** via the standard transcription endpoint.

- Send the buffered utterance as a single multipart upload of 16 kHz mono s16le wrapped in a WAV header (constructed in-memory; no temp files).
- Typical latency 1–3 s for utterances ≤ 15 s. Accuracy is the reference.
- `language` left as auto-detect (Italian / English mirroring per operator preference).

**Fallback: Dashscope `qwen2.5-omni-7b`** via the existing `lib/voice/asr.py` interface.

- Triggered if Whisper fails (network / 5xx / 429) or doesn't return within `asr.fallback_after_seconds`.
- Heavier (15–30 s) but already wired in JC. Worth keeping for resilience.

**Not for v1:** local ASR (whisper.cpp on the bridge host). May be added later if operator wants 100% offline path.

## Multi-agent routing

The router takes a transcript and produces `(agent_slug, message_text)`.

### Resolution order

1. **Explicit prefix.** Regex on the first ~6 tokens for one of:
   - `^(?:tell|ask|send (?:to|message to))\s+(<alias>)[,:\s]+(.*)$`
   - `^(<alias>)[,:\s]+(.*)$` (e.g., "Harold, what's the read on...")
   Where `<alias>` is any alias listed in `router.agents.*.aliases`. Case-insensitive.
2. **Conversation continuity.** If no prefix and the previous utterance from the same `mic_on` session was routed to agent X, route to X.
3. **Default agent.** `router.default_agent`.

### Multi-recipient broadcast

If the operator says `"tell everyone <msg>"`, send the message to **every** configured agent. Replies stream back in arrival order; the app paginates them as separate cards (each card prefixed with agent name).

### Negative cases — explicit errors back to glasses

- Prefix names an alias that does not resolve → `error { code: "unknown_agent", message: "no agent matches '<alias>'" }`.
- No agent configured at all → `error { code: "no_agents_configured" }`.

## Source tagging

Every message sent to JC agents from the bridge is prefixed with the literal token `[glasses]` followed by a space, then the transcript.

Example:

```
[glasses] read on the Iran insurance corridor today
```

### Rationale

- JC personas can detect the tag in incoming text and adjust formatting: shorter replies, no MarkdownV2 ornamentation that doesn't render on the 4-bit display, prioritize "what to do next" over background.
- Optional per-persona handling. Harold may treat `[glasses]` as a signal to switch into a tighter format (e.g., max 3 short labeled fields). Implementation of this is **inside the persona's STYLE.md / RULES.md**, not in this spec.
- Tag is deterministic and trivial to grep/strip downstream if needed.

### Out of scope for this spec

- Modifying any persona's instructions to *act* on the tag. That's an instance-level change the operator makes after the bridge ships.

## Notification model — replacing "OS push"

The G2 SDK has no documented OS-level push API to wake a closed app. The bridge does not need one because:

1. The app holds an open WebSocket while foregrounded.
2. The operator's interaction pattern is: glance at glasses, speak, wait, read. The app is foregrounded during the wait. There is no "answer arrives an hour later while phone is in pocket" requirement.
3. If the WebSocket drops mid-wait, the bridge keeps the agent reply in its retry buffer (`reply_id`-keyed) for `reply_window_seconds`. On reconnect, the unacked reply is pushed immediately and the operator sees it as soon as the app reopens.

### Open decision (see below)

If real OS-level push becomes a requirement, two paths exist:
(a) Push via the operator's phone OS using Even Hub's eventual background-app facilities (not currently documented).
(b) Mirror unacked replies to a separate Telegram chat the operator already gets push notifications from. Worst case but always works.

## Security

- **Telegram session.** Stored at `telegram.session_path`. File mode `0600`, owner = bridge service user. Not in the repo, not in backups that leave the host. `bridge auth` is the only way to (re)generate it; OTP + 2FA prompted on stdin. RULES §1 trust level T3 — explicit operator action.
- **Shared secret (`JC_GLASSES_BRIDGE_SECRET`).** 32 bytes, generated at install time, stored in the host env (systemd `EnvironmentFile`). Same secret embedded in the Even Hub app bundle at build time. Rotation = regenerate + rebuild app bundle + push update.
- **TLS.** WSS only. Self-signed not permitted (WebView CORS + cert pinning behavior on iOS/Android makes self-signed brittle). Use Let's Encrypt.
- **Domain whitelist.** Bridge's domain is the only entry in the Even Hub app's `app.json` network whitelist. No other outbound destinations from the app.
- **No third-party services in the audio path.** Whisper is the operator's own OpenAI key. Dashscope is the operator's own Dashscope key. Both already used by JC.
- **No audio recording on disk** by default. PCM frames are held only in the per-utterance in-memory buffer. The WAV blob handed to ASR is discarded after the response. `debug_record_utterances: true` writes WAVs to a configurable path only when the operator explicitly enables it for troubleshooting.
- **Bridge auth identity.** The shared-secret auth is sufficient because the entire chain is operator-only (one app, one bridge, one Telegram account). The bridge is **not** a multi-tenant service.

## Operational

- **Systemd unit** at `bridge/jc-glasses/systemd/jc-glasses-bridge.service`. `Restart=on-failure`, `RestartSec=5`. `EnvironmentFile=/etc/jc-glasses-bridge/env`.
- **Host:** runs alongside the existing JC instance gateways on the operator's home server. No co-location requirement with any specific persona instance — the bridge is a peer process.
- **Health:** `bridge run` exposes a small HTTP `/healthz` on localhost (separate port from the WSS) that returns `200` if Telethon is connected and the WS server is accepting. The existing `jc watchdog` can be extended later to watch this.
- **Observability:** structured JSON logs to stdout → journald. Operator runs `journalctl -u jc-glasses-bridge -f` to tail.

## Testing

- **Unit:** `frames.py` schema round-trips, router prefix parsing, agent alias resolution, source-tag prefixing.
- **Integration (no glasses):** `bridge test-send harold "ping"` exercises ASR-skip + router + Telethon send + reply listener. CI-friendly with a mock Telethon client.
- **Simulator:** Even Hub `even-dev` simulator (`BxNxM/even-dev`) runs the Even Hub app bundle against a mock G2. Replay a canned PCM file as `audio` frames. Validates the full app → bridge → Telegram → reply → app loop without physical hardware.
- **Real glasses:** manual sign-off on:
   - Press-hold → speak → release → see transcript echo → see Harold reply.
   - Speak `"Rachel, what's on the calendar?"` → reply renders from Rachel.
   - Drop WiFi mid-reply → reconnect → reply arrives via retry buffer.
   - 90-second reply (force Harold to delay) → app stays on `THINK` → reply renders when ready.

## Build estimate (refresher)

| Component | Estimate |
|---|---|
| Bridge (Python: WS server + Whisper + Telethon + router) | 1.5–2 days |
| Even Hub app (TS + SDK: mic, WS, display, paginate) | 1.5–2 days |
| Multi-agent routing + source tagging | 0.5 day |
| Integration on simulator + real glasses | 1 day |
| Hardening (auth, TLS, systemd, logs, retry buffer) | 0.5–1 day |
| **Total** | **5–6.5 days** |

## Open decisions (owner sign-off needed before implementation starts)

1. **Hosting.** Bridge runs on the same host as the JC gateways, or a separate host? Default: same host. Same-host avoids one more box to maintain; security profile is identical (operator-only).
2. **Domain + TLS cert.** Which subdomain hosts the WSS endpoint? Default: `g2.<operator-domain>` with Let's Encrypt.
3. **Default agent.** `default_agent: harold` is proposed. Operator may prefer `rachel` if executive/scheduling tasks dominate.
4. **"Tell everyone" broadcast.** Ship in v1 (per spec above) or defer? Defer keeps the v1 surface smaller.
5. **Continuity within a mic session.** Should "no prefix" route to the last-addressed agent, or always to default? Spec proposes "last in same mic session", reset between sessions.
6. **Background OS push.** Required for v1, or accept "foreground only"? Spec assumes foreground only.
7. **Per-persona `[glasses]` handling.** Out of this spec, but implementer should open a follow-up issue per active persona instance to update STYLE.md / RULES.md to format for the 3×50 display.
8. **Even Hub app distribution.** Even Hub has an app store (launched 2026-04-03). Publish there, or sideload via simulator-style developer install? Operator-only install is fine for v1.

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
- Even Hub simulator: https://github.com/BxNxM/even-dev
- Even Demo App (G1, useful for BLE protocol reference): https://github.com/even-realities/EvenDemoApp
- Telethon docs: https://docs.telethon.dev
- OpenAI Whisper transcription API: https://platform.openai.com/docs/guides/speech-to-text
- Existing JC voice ASR: `lib/voice/asr.py`
- Existing JC gateway config schema: `ops/gateway.yaml`, `docs/specs/unified-gateway-0.3.0-remaining.md`
- OpenClaw bridge (explicitly NOT used, kept here as the rejected alternative): https://github.com/dAAAb/openclaw-even-g2-bridge-skill
