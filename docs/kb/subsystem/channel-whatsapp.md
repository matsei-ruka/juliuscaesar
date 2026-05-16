---
title: WhatsApp channel
section: subsystem
status: active
last_verified: 2026-05-16
verified_by: Matsei Ruka
code_anchors:
  - path: lib/gateway/channels/whatsapp.py
    symbol: WhatsAppChannel
  - path: lib/gateway/channels/whatsapp_sidecar.py
    symbol: WhatsAppSidecar
  - path: lib/gateway/channels/whatsapp_policy.py
    symbol: WhatsAppPolicy
  - path: lib/gateway/channels/whatsapp_state.py
  - path: lib/gateway/channels/whatsapp_protocol.py
  - path: lib/gateway/channels/whatsapp_sidecar/src/index.ts
  - path: lib/gateway/channels/whatsapp_sidecar/src/socket.ts
    symbol: startSocket
  - path: lib/gateway/channels/whatsapp_sidecar/src/normalize.ts
    symbol: normalizeMessage
  - path: lib/gateway/channels/whatsapp_sidecar/src/auth.ts
    symbol: buildAuthState
  - path: lib/gateway/channels/whatsapp_sidecar/src/send.ts
    symbol: sendMessage
  - path: bin/jc-whatsapp
related:
  - subsystem/channel-email.md
  - subsystem/gateway-queue.md
  - contract/brain-capabilities.md
---

# WhatsApp channel

## What it is

A first-class JuliusCaesar gateway channel for WhatsApp messaging. Uses a Node.js sidecar (`Baileys` v7, WhatsApp Web library) connected via QR code to a dedicated WhatsApp number. The Python gateway owns all policy, queue routing, and delivery — the sidecar only manages the WhatsApp Web socket and message normalization. Access control mirrors the email channel's 3-tier pattern: Trusted, External, Blocked.

## Key invariants

1. **The sidecar must never make policy decisions.** It normalizes inbound Baileys messages and emits JSON. The Python channel decides whether a sender is Trusted/External/Blocked, whether a group message passes the mention gate, and whether a response is sent, drafted, or dropped.
2. **Blocked always wins.** A sender in `blocklist` is blocked regardless of `dm_policy`, `group_policy`, or group tier. The only override chain is: blocklist → allow_from → dm_policy default.
3. **External senders require operator approval before a reply is sent.** The brain still processes the message, but the response is drafted to `state/channels/whatsapp/drafts/`. The operator is notified via Telegram (best-effort) and must run `jc-whatsapp chats trust <jid>` or `jc-whatsapp chats block <jid>`.
4. **Auth state is Baileys-native, never wrapped.** Baileys writes its multi-file auth (creds.json + SignalKeyStore keys/) atomically. The `auth.ts` module reads/writes with tmp→rename semantics. No second wrapper layer.
5. **The sidecar restarts with exponential backoff (5s → 30s → 120s).** After 3 crashes, it gives up and emits a fatal error. The WhatsApp channel's `health()` method reports `auth_valid` based on whether the fatal error is `auth_missing` or starts with `logged_out`.
6. **Media download sets `meta.image_path` before enqueue.** The brain receives the expected path even if the download is still in-flight. The download result is handled asynchronously by `_on_download_result`.

## Architecture

```
WhatsApp app
  └── Linked Devices QR scan
        │
        ▼
  Node.js sidecar (Baileys v7, WhatsApp Web socket)
  lib/gateway/channels/whatsapp_sidecar/src/
    ├── index.ts       ← stdio JSON line protocol loop
    ├── socket.ts      ← Baileys makeWASocket lifecycle
    ├── auth.ts        ← atomic creds + SignalKeyStore persistence
    ├── normalize.ts   ← WAMessage → normalized JSON
    └── send.ts        ← outbound text (3500-char chunked) + media download
        │
        │ stdio JSON lines (one per line, both directions)
        ▼
  Python channel
  lib/gateway/channels/
    ├── whatsapp.py           ← WhatsAppChannel class (matches EmailChannel pattern)
    ├── whatsapp_protocol.py  ← JSON encode/decode + dataclass types
    ├── whatsapp_policy.py    ← Trusted/External/Blocked + group mention gate
    ├── whatsapp_state.py     ← chats.jsonl, drafts/, events.jsonl
    └── whatsapp_sidecar.py   ← process lifecycle with restart/backoff
        │
        ▼
  Gateway queue → triage → brain → delivery → sidecar send → WhatsApp
```

Sidecar protocol direction:

| Direction | Event types |
|-----------|------------|
| sidecar → Python (stdout) | `qr`, `connection`, `message`, `send_result`, `download_result`, `error` |
| Python → sidecar (stdin) | `send`, `download`, `stop` |

## Config

```yaml
channels:
  whatsapp:
    enabled: false
    accounts:
      default:
        auth_dir: state/channels/whatsapp/auth/default
        dm_policy: external         # trusted | external | blocked
        allow_from: []              # JIDs always Trusted
        blocklist: []               # JIDs always Blocked
        group_policy: external
        group_allow_from: []        # Group JIDs allowed to receive
        require_group_mention: true
        media:
          enabled: true
          max_bytes: 25000000
```

## Mini recipe — add WhatsApp to a new instance

```
1. jc doctor                           # confirms Node 18+ available
2. Edit ops/gateway.yaml:
   channels.whatsapp.enabled: true
   channels.whatsapp.accounts.default.dm_policy: external
3. jc whatsapp login                   # scan QR from WhatsApp → Linked Devices
4. jc whatsapp status                  # verify connected, self_jid
5. Send a test DM from a trusted number
6. jc whatsapp chats trust <jid>       # promote the test sender
7. Verify reply arrives on WhatsApp
```

## Gotchas

- **`pi` prefix stripping in the adapter isn't used here.** The sidecar receives model names directly via `--model`. JC brain specs like `pi:sonnet` are stripped by the adapter shell script, not by the WhatsApp channel. The WhatsApp channel is a *transport*, not a brain — model selection is a brain-layer concern.
- **`health().auth_valid` uses `startswith("logged_out")`.** The fatal error string is `"logged_out: <reason>"`, so exact-match `"logged_out"` fails. The prefix check was added in a post-audit fix.
- **Chat record `jid` differs from `conversation_id` for groups.** Chat records use the group JID (one record per group). Conversation IDs use the sender JID (one transcript per sender). This is intentional — don't try to unify them.
- **The sidecar must be built before first use.** `jc doctor` checks for `dist/index.js`. Run `cd lib/gateway/channels/whatsapp_sidecar && npm install && npm run build` manually or via `jc doctor --fix`.

## Open questions / known stale

- **2026-05-16**: Media download is async (initiated after enqueue). The brain sees `image_path` in meta but the file may not have arrived yet. If the download fails, the brain gets an ENOENT. A future improvement would be synchronous download or a retry queue.
- **2026-05-16**: Operator notification for External senders uses Telegram directly via `send_telegram.py`. This assumes Telegram is the primary operator channel. Multi-channel operators would need a generic notification interface.

## See also

- `subsystem/channel-email.md` — the 3-tier access control pattern originated here
- `subsystem/gateway-queue.md` — how events flow through the queue after enqueue
- `contract/brain-capabilities.md` — capability matrix (WhatsApp is a channel, not a brain)
