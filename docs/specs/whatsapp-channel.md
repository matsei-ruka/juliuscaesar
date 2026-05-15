# Spec: WhatsApp channel

**Status:** Draft
**Date:** 2026-05-14
**Branch base:** `main`
**Owner:** TBD — assign before merge

## Goal

Add WhatsApp as a first-class JuliusCaesar gateway channel. The implementation
uses Baileys (WhatsApp Web library) in a narrow Node.js sidecar, communicating
with the Python gateway over stdio JSON lines. Access control follows the
established email channel pattern: Trusted, External, Blocked.

This is a spec-only PR. It contains no runtime code.

## Context

`@openclaw/whatsapp@2026.5.7` is an OpenClaw channel plugin using Baileys.
JuliusCaesar must not import it. The OpenClaw plugin is tightly coupled to its
plugin SDK, runtime registry, account abstractions, pairing store, outbound
adapter surface, and setup flow. Importing it would embed a second runtime
architecture inside the gateway instead of building native to JC's channel
contracts.

The correct approach: use Baileys directly in a narrow sidecar, keep Python
gateway ownership of queues, routing, delivery, transcripts, policy, and
recovery. Port product concepts, not code.

## Design decisions (pre-approved)

These were decided before this spec was written and are non-negotiable:

| # | Decision |
|---|----------|
| 1 | Sidecar lives in `lib/gateway/channels/whatsapp_sidecar/` with its own `package.json`. Self-contained, no global npm dependency. |
| 2 | Node 18+ checked by `jc doctor` only when `channels.whatsapp.enabled: true`. |
| 3 | Multi-account from day one. Config uses `accounts:` dict. State paths use `<account_id>/` prefix. First account is `default`. |
| 4 | Access control follows the email channel pattern: **Trusted** (commands, direct reply), **External** (response drafted, operator notified with proposed answer for approval), **Blocked** (silent drop). |
| 5 | Baileys manages auth state natively. JC does not wrap it in a single `creds.json`. |
| 6 | CLI: both a separate `bin/jc-whatsapp` binary AND subcommands under the main `jc` router. |
| 7 | QR display: terminal rendering, `--print-qr` (text), and `--send-qr-to-telegram` (image to operator). |
| 8 | Media handling reuses existing Telegram media patterns in `lib/gateway/channels/telegram_media.py`. |
| 9 | Group mention detection: sidecar normalizes mention data from Baileys; Python channel applies the mention gate policy. |
| 10 | Phase 1 is this spec. No code. |

## Product promise

WhatsApp support is commercially useful when an operator can:

- Connect a dedicated WhatsApp number by scanning a QR code.
- Receive DMs from Trusted contacts and allowed group mentions through the
  normal gateway.
- Have External (unknown) contacts trigger operator notification with a
  proposed response for approval.
- Block unwanted senders silently.
- Send replies through the same linked WhatsApp session.
- Recover when WhatsApp logs out or the socket disconnects.
- Inspect queue events, transcripts, and delivery failures after the fact.

## Non-goals

- Do not build a general WhatsApp client.
- Do not ship the official Meta Cloud API in the first milestone (architecture
  leaves the path open: `whatsapp_cloud` as a separate channel backend or a
  backend under the same `whatsapp` channel — decide later).
- Do not import `@openclaw/whatsapp` or any OpenClaw plugin as a black box.
- Do not copy OpenClaw source without explicit license and attribution review.
- Do not require public OpenAI or Meta credentials for the WhatsApp Web path.
- Do not silently process arbitrary groups. Group messages require both
  allowlist and mention (or explicit per-group override).
- Do not process messages from unknown DMs unless External policy allows it
  (with operator approval).
- Do not bypass the gateway queue, triage, recovery, transcripts, or watchdog.
- Do not promise that WhatsApp Web is a formally supported WhatsApp Business
  API.

## Architecture

```
WhatsApp app
  -> Linked Devices QR scan
  -> Baileys WhatsApp Web socket (Node.js sidecar)
       │
       │ stdio JSON lines
       ▼
  Python channel (whatsapp.py)
       │
       ├── Access policy (Trusted/External/Blocked — reuses email pattern)
       │
       ▼
  Gateway queue -> triage -> brain -> delivery -> sidecar -> WhatsApp
```

### Why stdio over local HTTP

- No port allocation, no listener to secure.
- Easy lifecycle ownership by the Python channel (start/stop subprocess).
- Straightforward test fakes (pipe a JSON producer).
- Aligns with existing native CLI orchestration style (adapters, workers).

### Sidecar boundaries

The sidecar owns **only** WhatsApp Web socket mechanics:
- Baileys socket lifecycle (connect, reconnect, keepalive).
- Auth state persistence (Baileys native multi-file format).
- QR code generation during login.
- Connection state reporting.
- Inbound message normalization.
- Outbound message sending.

The sidecar must **not** know about:
- JuliusCaesar brains, routing, triage classes, transcripts.
- Gateway queue format.
- Access control policies.
- Operator notification channels.

## Component plan

### Python channel

New files:

```
lib/gateway/channels/whatsapp.py            — Channel class (matches EmailChannel pattern)
lib/gateway/channels/whatsapp_protocol.py   — Sidecar JSON protocol encode/decode
lib/gateway/channels/whatsapp_policy.py     — Access control (matches email_policy.py pattern)
lib/gateway/channels/whatsapp_state.py      — Local state (chats, pending senders, events)
lib/gateway/channels/whatsapp_sidecar.py    — Sidecar process lifecycle (start/stop/supervise)
```

Responsibilities:
- Load `channels.whatsapp` config from `ops/gateway.yaml`.
- Start/stop/restart the sidecar subprocess.
- Receive normalized inbound JSON lines from sidecar stdout.
- Apply Trusted/External/Blocked policy per sender and per group.
- Enqueue accepted events into the gateway queue (`source: whatsapp`).
- Draft External responses for operator approval.
- Send approved outbound replies through sidecar stdin.
- Expose login/logout/status operations via `jc-whatsapp` CLI.
- Emit clear errors for watchdog and operator logs.

### Node sidecar

New directory:

```
lib/gateway/channels/whatsapp_sidecar/
  package.json          — Baileys dependency, Node 18+ engines
  src/index.ts          — Entry point, stdio JSON line protocol loop
  src/socket.ts         — Baileys socket lifecycle
  src/auth.ts           — Auth state persistence (Baileys native)
  src/normalize.ts      — Inbound Baileys message → JC normalized JSON
  src/send.ts           — Outbound send commands → Baileys sendMessage
```

Responsibilities:
- Create a Baileys socket with persisted auth state.
- Emit QR updates (`{"type":"qr","qr":"..."}`) during login.
- Emit connection updates (`{"type":"connection","state":"open"}`).
- Normalize `messages.upsert` into a small JSON schema (see below).
- Accept `{"type":"send",...}` commands on stdin.
- Save auth state atomically (Baileys native multi-file).
- Report `logged_out`, `reconnecting`, `auth_missing`, `send_failed` states.

### CLI

Separate binary + `jc` subcommands:

```bash
# Dedicated binary
jc-whatsapp status [--json]
jc-whatsapp login [--account default] [--print-qr] [--send-qr-to-telegram]
jc-whatsapp logout [--account default]
jc-whatsapp send <to> <text> [--account default]
jc-whatsapp chats list [--json]
jc-whatsapp chats trust <jid>
jc-whatsapp chats external <jid>
jc-whatsapp chats block <jid>
jc-whatsapp doctor

# Also via main jc router
jc whatsapp status [--json]
jc whatsapp login [--account default] [--print-qr] [--send-qr-to-telegram]
jc whatsapp logout [--account default]
jc whatsapp chats list [--json]
jc whatsapp chats trust <jid>
jc whatsapp chats external <jid>
jc whatsapp chats block <jid>
```

## Configuration

`ops/gateway.yaml` shape:

```yaml
channels:
  whatsapp:
    enabled: false
    accounts:
      default:
        auth_dir: state/channels/whatsapp/auth/default
        dm_policy: external       # trusted | external | blocked
        allow_from: []            # JIDs always treated as Trusted
        blocklist: []             # JIDs always treated as Blocked
        group_policy: external    # trusted | external | blocked
        group_allow_from: []      # Group JIDs that may receive messages
        require_group_mention: true
        media:
          enabled: true
          max_bytes: 25000000
        socket:
          connect_timeout_seconds: 30
          keepalive_seconds: 20
          restart_backoff_seconds: [5, 30, 120]
        history_limit: 20
```

Policy semantics (per account):

| Tier | DM behavior | Group behavior |
|------|-------------|----------------|
| **Trusted** | Brain responds. Reply sent immediately. | Brain responds if group is in `group_allow_from` AND message mentions assistant. Reply sent immediately. |
| **External** | Message enqueued. Brain response drafted. Operator notified with proposed answer for approval. On approval → sent. On denial → dropped with log. | Same as DM External, plus `group_allow_from` and mention gate. |
| **Blocked** | Silent drop. Logged. | Silent drop. Logged. |

**Default policy**: all accounts default to `dm_policy: external` and
`group_policy: external` with `require_group_mention: true`. This is the safe
start — nothing goes out without operator awareness.

### Access control flow (matches email pattern)

```
WhatsApp inbound DM
  │
  ├── sender in allow_from? → Trusted → enqueue → brain responds → send reply
  │
  ├── sender in blocklist? → Blocked → silent drop + log
  │
  └── otherwise → External →
        enqueue event (brain still processes)
        brain response → drafted (NOT sent)
        operator notified via principal channel (Telegram)
        operator approves:
          ├── jc-whatsapp chats trust <jid> → sender moved to Trusted, draft sent
          └── jc-whatsapp chats block <jid> → sender moved to Blocked, draft dropped
```

The operator notification for External senders includes:
- Sender JID and push name.
- Message content.
- The brain's proposed response.
- Quick actions: Trust / Block (via Telegram inline buttons if on Telegram).

## Login lifecycle

```
jc-whatsapp login --account default [--print-qr] [--send-qr-to-telegram]
  │
  ├── Python CLI starts sidecar in login mode
  ├── Sidecar creates Baileys socket (no existing auth)
  ├── Sidecar emits QR event on stdout:
  │     {"type":"qr","account_id":"default","qr":"..."}
  ├── Terminal: renders QR code (qrcode-terminal or similar)
  ├── --print-qr: outputs raw QR string for external rendering
  ├── --send-qr-to-telegram: sends QR image to operator via Telegram
  ├── Operator scans from WhatsApp → Linked Devices
  ├── Sidecar persists Baileys auth state natively
  ├── Sidecar emits connection open:
  │     {"type":"connection","state":"open","account_id":"default",
  │      "self_jid":"123@s.whatsapp.net"}
  └── CLI exits 0
```

## Inbound lifecycle

```
Baileys messages.upsert
  │
  ├── Sidecar filters out:
  │     - fromMe messages (unless self-chat explicitly enabled)
  │     - Protocol messages (receipts, status, presence)
  │     - Broadcast/status JIDs
  │     - Historical messages older than connection time minus grace window
  │
  ├── Sidecar normalizes and emits JSON line on stdout:
  │
  │  {
  │    "type": "message",
  │    "account_id": "default",
  │    "message_id": "ABCDEF",
  │    "remote_jid": "15551234567@s.whatsapp.net",
  │    "sender_jid": "15551234567@s.whatsapp.net",
  │    "chat_type": "dm",
  │    "from_me": false,
  │    "push_name": "Alice",
  │    "timestamp": "2026-05-14T12:00:00Z",
  │    "text": "hello",
  │    "mentions": ["123@s.whatsapp.net"],
  │    "quoted_message_id": null,
  │    "media": null,
  │    "raw_kind": "conversation"
  │  }
  │
  ├── Python channel applies access policy
  ├── Accepted → gateway queue event with:
  │     source: "whatsapp"
  │     source_message_id: "<account_id>:<remote_jid>:<message_id>"
  │     user_id: sender_jid
  │     conversation_id: "whatsapp:<account_id>:<remote_jid>"
  │     content: normalized text + media placeholders
  │     meta.delivery_channel: "whatsapp"
  │     meta.account_id: account_id
  │     meta.chat_id: remote_jid
  │     meta.sender_jid: sender_jid
  │     meta.chat_type: "dm" or "group"
  │     meta.push_name: sender's WhatsApp display name
  │     meta.mentions: list of mentioned JIDs
  │
  └── Dedup uses (source, source_message_id)
```

## Outbound lifecycle

```
Gateway has assistant response
  │
  ├── Delivery layer resolves WhatsApp channel
  ├── whatsapp.py checks sender tier:
  │
  │   Trusted:
  │     ├── Format text (chunk >3500 chars, convert Markdown to WhatsApp-safe)
  │     ├── Send via sidecar: {"type":"send","to":"...","text":"..."}
  │     └── Sidecar returns: {"type":"send_result","ok":true,"message_id":"..."}
  │
  │   External:
  │     ├── Draft response to state/channels/whatsapp/drafts/<id>.json
  │     ├── Notify operator with proposed answer
  │     └── Await approval (trust/block)
  │
  │   Blocked:
  │     └── Log + skip
```

Sidecar send command:

```json
{
  "id": "cmd_123",
  "type": "send",
  "account_id": "default",
  "to": "15551234567@s.whatsapp.net",
  "text": "Hello",
  "quoted_message_id": "ABCDEF",
  "media": null
}
```

Sidecar response:

```json
{
  "id": "cmd_123",
  "type": "send_result",
  "ok": true,
  "message_id": "3EB0..."
}
```

### Text formatting

- Convert Markdown tables to plain bullets or code blocks before send.
- WhatsApp formatting is different from Telegram's MarkdownV2. Do not reuse
  the Telegram escaper.
- Chunk replies longer than 3500 characters.
- No rich formatting in v1. Plain text with basic `*bold*`, `_italic_`,
  `~strikethrough~` only where unambiguous.

## Media (reuses Telegram media patterns)

Follow the patterns established in `lib/gateway/channels/telegram_media.py`:

- Inbound image/audio metadata recognized by the sidecar and included in the
  normalized message (`media` field).
- Media downloaded to instance state only when `media.enabled: true`.
- Image events set `meta.image_path` so existing vision routing can apply
  (same as Telegram).
- Audio events bridged to existing voice ASR (`lib/voice/asr.py`) when the
  file format is compatible.
- Max bytes enforced before writing (`media.max_bytes`).

State path:

```
state/channels/whatsapp/media/<account_id>/<message_id>/<filename>
```

Security:
- Generate safe filenames (UUID-based, no user-controlled names).
- Store MIME type and SHA256 digest in metadata.
- Do not execute or parse documents in the first release.

## Group messages

Group message accepted only when ALL conditions are met:

1. Group JID is in `group_allow_from` for the account.
2. Message mentions the assistant's JID, quotes the assistant, OR
   `require_group_mention: false` for that group.
3. Sender is not in `blocklist`.
4. Sender tier is not Blocked.

The sidecar normalizes mention data from Baileys `messages.upsert`. The Python
channel applies the mention gate. The sidecar does not make policy decisions.

## Recovery and watchdog integration

Sidecar emits health signals on stdout:

```json
{"type":"connection","state":"reconnecting","reason":"..."}
{"type":"connection","state":"logged_out","reason":"session_expired"}
{"type":"error","fatal":true,"reason":"auth_missing"}
{"type":"send_result","ok":false,"error":"..."}
```

Gateway behavior:

| Signal | Action |
|--------|--------|
| `logged_out` | Notify operator. Mark channel unhealthy. Suggest `jc whatsapp login`. |
| `reconnecting` | Log. Wait. No operator action needed (Baileys handles it). |
| `auth_missing` | Fatal. Channel stops. Operator must run `jc whatsapp login`. |
| `send_failed` | Mark event failed with channel-auth error. Do not retry brain. |
| Sidecar crashed | Python channel restarts with exponential backoff from config. After 3 failures in a window, mark unhealthy and notify operator. |

Watchdog should:
- Recognize WhatsApp health signals as a channel problem, not a brain problem.
- Never switch brains because WhatsApp outbound is down.
- Include WhatsApp auth status in `jc doctor` output.

## State and files

```
state/channels/whatsapp/
  auth/<account_id>/                    — Baileys native auth files
  chats.jsonl                           — Known chats (JID, push name, tier, last_seen)
  pending_senders.json                  — External senders awaiting approval
  drafts/<id>.json                      — Drafted responses from External senders
  media/<account_id>/<message_id>/      — Downloaded media files
  events.jsonl                          — Channel event log
  sidecar.log                           — Sidecar stderr
```

## Security

- WhatsApp Web via Baileys is not the official WhatsApp Business API.
- Recommend a dedicated assistant number, not the operator's personal number.
- Document that Linked Devices can be revoked from the WhatsApp mobile app.
- Avoid bulk messaging, scraping, or cold outbound use cases.
- Rate-limit outbound sends (configurable, default 1/second).
- Expose `logout` and auth status clearly.
- Auth files: let Baileys manage natively. Do not add a second wrapping layer.
  Use mode 600 on the auth directory.
- Media files: enforce max bytes, safe filenames, never execute.
- Sidecar process: stdin/stdout only. No network listener.
- Never log credentials, auth tokens, or QR data to events.jsonl.

## Implementation phases

### Phase 1 — Spec (this PR)

This document. No code.

### Phase 2 — Sidecar skeleton

**Files:** `lib/gateway/channels/whatsapp_sidecar/` (full Node package)

- `package.json` with Baileys dependency, Node 18+ engines.
- `src/index.ts` — stdio JSON line protocol loop.
- `src/socket.ts` — Baileys socket lifecycle.
- `src/auth.ts` — Auth state persistence (Baileys native).
- `src/normalize.ts` — Inbound message → normalized JSON (no policy, just shape).
- `src/send.ts` — Outbound send commands.

**Acceptance:**
- `node src/index.ts` starts, emits QR, waits for scan.
- After scan, emits `{"type":"connection","state":"open",...}`.
- Auth state persisted and reloaded on restart (no re-scan).
- `Ctrl+C` clean shutdown.

### Phase 3 — Python channel + inbound DM path

**Files:** `lib/gateway/channels/whatsapp.py`, `whatsapp_protocol.py`,
`whatsapp_sidecar.py`, `whatsapp_state.py`, `whatsapp_policy.py`

- Python channel starts/stops sidecar as subprocess.
- Normalized inbound messages flow through access policy.
- Trusted DMs enqueue gateway events.
- External DMs enqueue + draft response.
- Blocked DMs silently dropped.
- Text replies sent back through sidecar for Trusted senders.

**Acceptance:**
```bash
pytest tests/gateway/channels/test_whatsapp.py
```

Test cases:
- Sidecar starts, emits connection open, Python channel reads it.
- Trusted DM → enqueued, response sent.
- External DM → enqueued, response drafted, NOT sent.
- Blocked DM → silently dropped.
- Group message with mention → enqueued.
- Group message without mention → dropped.
- Sidecar crash → Python restarts with backoff.

### Phase 4 — CLI + approval flow

**Files:** `bin/jc-whatsapp`, `jc` router extension

- `jc whatsapp login` with QR terminal/print/telegram options.
- `jc whatsapp logout`.
- `jc whatsapp chats list/trust/external/block`.
- Operator notification for External senders with approve/deny actions.
- Draft → send on approval; draft → drop on denial.

**Acceptance:**
```bash
jc whatsapp login --print-qr    # outputs QR string
jc whatsapp chats list --json   # lists known chats with tiers
jc whatsapp chats trust <jid>   # moves sender to Trusted, sends pending draft
jc whatsapp chats block <jid>   # moves sender to Blocked, drops pending draft
```

### Phase 5 — Media + group hardening + watchdog

- Inbound images (reuse Telegram media patterns).
- Audio → ASR bridge if format compatible.
- Group allowlist + mention gate fully tested.
- Watchdog health integration.
- Reconnect/backoff production hardening.
- `jc doctor` WhatsApp checks.

## Open questions

1. **Meta Cloud API path**: should it be a parallel channel (`whatsapp_cloud`)
   or a backend under the same `whatsapp` channel config? Defer — architecture
   supports either.
2. **Self-chat mode**: should the assistant respond to its own messages
   (fromMe)? Default: no. Configurable later.
3. **Operator notification channel**: the spec assumes Telegram as the primary
   operator channel (via `principal.telegram_chat_id`). Should we build a
   generic notification interface for multi-channel operators? Defer to v2.
4. **Rate limiting granularity**: per-account, per-JID, or global? Start with
   global 1 msg/sec. Refine later.
5. **Node.js bundling**: should the sidecar src be TypeScript compiled at
   install time or shipped pre-compiled? Recommend: `jc doctor --fix` runs
   `npm install && npm run build` in the sidecar directory when
   `channels.whatsapp.enabled: true`.
6. **Multiple WhatsApp numbers (multi-account)**: the config supports it from
   day one (`accounts:` dict). But the sidecar process model needs a decision:
   one sidecar process per account, or one process multiplexing all accounts
   on a single Baileys socket? Start with one process per account (simpler,
   isolates failures). Config `accounts.<id>` maps 1:1 to a sidecar instance.

## Definition of done

WhatsApp is a first-class channel when:

- Operator can `jc whatsapp login` and scan a QR code from WhatsApp mobile.
- Auth persists across restarts (no re-scan).
- Trusted DMs flow through gateway: receive → brain → reply sent.
- External DMs trigger operator notification with proposed answer.
- Operator can `jc whatsapp chats trust/block` to manage senders.
- Group messages require allowlist + mention (or explicit override).
- Media images route through existing vision pipeline.
- Sidecar recovers from disconnects (Baileys reconnect).
- Python channel restarts crashed sidecar with backoff.
- `jc doctor` reports WhatsApp auth, connection, and config status.
- Targeted tests pass:

```bash
pytest tests/gateway/channels/test_whatsapp.py
```
