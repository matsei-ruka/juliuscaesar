# WhatsApp Channel Spec

Status: Spec only
Date: 2026-05-12
Branch: `codex/whatsapp-channel-spec`

## Goal

Make WhatsApp a first-class JuliusCaesar gateway channel.

The first production path should use WhatsApp Web through Baileys, linked by QR
code from the operator's WhatsApp app. The implementation should be native to
JuliusCaesar's gateway/channel contracts, while using OpenClaw's WhatsApp plugin
as a design reference rather than importing its runtime wholesale.

This is a spec-only PR. It intentionally contains no runtime code.

## Context

`@openclaw/whatsapp@2026.5.7` is an OpenClaw channel plugin. Its package depends
on `@whiskeysockets/baileys@7.0.0-rc.9`, not the official Meta Cloud API. Its
core pattern is:

```text
WhatsApp app
  -> Linked Devices QR scan
  -> Baileys WhatsApp Web socket
  -> persisted multi-file auth state
  -> messages.upsert inbound stream
  -> sendMessage outbound path
```

That pattern is the right starting point. The OpenClaw package itself should not
be imported directly into JuliusCaesar because it is coupled to OpenClaw's plugin
SDK, runtime registry, account abstractions, pairing store, outbound adapter
surface, and setup flow. Importing it would trade short-term speed for a second
runtime architecture inside the gateway.

The JuliusCaesar implementation should instead:

- use Baileys directly in a narrow Node sidecar;
- keep Python gateway ownership of queues, routing, delivery, transcripts, and
  recovery;
- copy no OpenClaw source unless license and attribution are explicitly reviewed;
- port only product concepts that fit JuliusCaesar.

## Product Promise

WhatsApp support is commercially useful when an operator can:

- connect a dedicated WhatsApp number by scanning a QR code;
- receive DMs and allowed group mentions through the normal gateway;
- send replies through the same linked WhatsApp session;
- approve or block senders and groups safely;
- recover when WhatsApp logs out or the socket disconnects;
- inspect queue events, transcripts, and delivery failures after the fact.

## Non-Goals

- Do not build a general WhatsApp client.
- Do not ship the official Meta Cloud API in the first milestone.
- Do not import the OpenClaw plugin as a black box.
- Do not require public OpenAI or Meta credentials for the WhatsApp Web path.
- Do not silently process arbitrary groups.
- Do not process messages from unknown DMs unless the configured policy allows
  it.
- Do not bypass the gateway queue, triage, recovery, transcripts, or watchdog.
- Do not promise that WhatsApp Web is a formally supported WhatsApp Business API.

## Architecture

Baileys is Node/TypeScript. The Python gateway should not embed Node in-process.
Use a small sidecar process with a narrow local protocol.

```text
lib/gateway/channels/whatsapp.py
  starts and supervises
    lib/gateway/channels/whatsapp_sidecar/
      Node process using Baileys
        WhatsApp Web socket
```

Recommended transport:

```text
Python channel <-> sidecar JSON lines over stdio
```

Why stdio over local HTTP:

- no port allocation;
- no listener to secure;
- easy lifecycle ownership by the Python channel;
- straightforward test fakes;
- aligns with existing native CLI orchestration style.

The sidecar owns only WhatsApp Web socket mechanics. The Python channel owns all
gateway-facing policy and persistence.

## Component Plan

### Python Channel

New files:

```text
lib/gateway/channels/whatsapp.py
lib/gateway/channels/whatsapp_protocol.py
lib/gateway/channels/whatsapp_state.py
```

Responsibilities:

- load `channels.whatsapp` config from `ops/gateway.yaml`;
- start/stop the sidecar when the channel is enabled;
- receive normalized inbound messages from sidecar stdout;
- apply DM and group access policy;
- enqueue accepted events into the gateway queue;
- send outbound replies through sidecar RPC;
- expose login/logout/status operations for CLI;
- emit clear errors for watchdog and operator logs.

### Node Sidecar

New directory:

```text
lib/gateway/channels/whatsapp_sidecar/
  package.json
  src/index.ts
  src/socket.ts
  src/auth.ts
  src/normalize.ts
  src/send.ts
```

Responsibilities:

- create a Baileys socket with persisted auth state;
- emit QR updates during login;
- emit connection updates;
- normalize inbound Baileys messages into a small JSON schema;
- accept outbound send commands;
- save auth state atomically;
- report logged-out, reconnecting, and fatal states.

The sidecar must not know about JuliusCaesar brains, routing, triage classes, or
transcripts.

## Configuration

Add `whatsapp` to `SUPPORTED_CHANNELS` and to the default channel config.

Minimal `ops/gateway.yaml` shape:

```yaml
channels:
  whatsapp:
    enabled: false
    account_id: default
    auth_dir: state/channels/whatsapp/auth/default
    dm_policy: pairing
    allow_from: []
    blocklist: []
    group_policy: allowlist
    group_allow_from: []
    require_group_mention: true
    history_limit: 20
    media:
      enabled: true
      max_bytes: 25000000
    socket:
      connect_timeout_seconds: 30
      keepalive_seconds: 20
      restart_backoff_seconds: [5, 30, 120]
```

Policy values:

- `dm_policy: pairing | allowlist | open | disabled`
- `group_policy: allowlist | open | disabled`

Default must be conservative:

- DMs use `pairing`.
- Groups use `allowlist`.
- Group messages require mention unless overridden per group in a later
  milestone.

Auth state lives under instance state by default. Operators may override
`auth_dir` for migration or shared deployment layouts, but secrets must never be
stored inside `ops/gateway.yaml`.

## CLI Surface

Prefer a dedicated command group:

```bash
jc whatsapp status [--json]
jc whatsapp login [--account default] [--print-qr]
jc whatsapp logout [--account default]
jc whatsapp send <to> <text>
jc whatsapp chats list [--json]
jc whatsapp chats approve <jid>
jc whatsapp chats deny <jid>
jc whatsapp doctor
```

Gateway compatibility:

- `jc gateway config` should show whether WhatsApp is enabled.
- `jc gateway work-once` should be able to process queued WhatsApp events.
- `jc chats` may eventually show WhatsApp chats, but first milestone can keep
  WhatsApp under `jc whatsapp chats`.

## Login Lifecycle

```text
jc whatsapp login
  -> Python CLI starts sidecar in login mode
  -> sidecar creates Baileys socket
  -> sidecar emits qr event
  -> CLI renders terminal QR
  -> operator scans from WhatsApp -> Linked Devices
  -> sidecar persists creds
  -> CLI exits success when connection opens
```

Sidecar events:

```json
{"type":"qr","account_id":"default","qr":"..."}
{"type":"connection","state":"open","account_id":"default","self_jid":"123@s.whatsapp.net"}
{"type":"connection","state":"close","account_id":"default","reason":"logged_out","status":401}
```

Auth file requirements:

- write atomically;
- chmod private where the platform supports it;
- keep a `creds.json.bak` after successful writes;
- never print creds or session keys;
- `logout` removes only WhatsApp auth files for the selected account.

## Inbound Lifecycle

```text
Baileys messages.upsert
  -> sidecar filters obvious non-user events
  -> sidecar emits normalized inbound JSON
  -> Python channel applies access policy
  -> accepted message becomes gateway queue event
  -> normal gateway dispatch, triage, recovery, delivery
```

Normalized inbound schema:

```json
{
  "type": "message",
  "account_id": "default",
  "message_id": "ABCDEF",
  "remote_jid": "15551234567@s.whatsapp.net",
  "sender_jid": "15551234567@s.whatsapp.net",
  "chat_type": "dm",
  "from_me": false,
  "push_name": "Alice",
  "timestamp": "2026-05-12T12:00:00Z",
  "text": "hello",
  "mentions": [],
  "quoted_message_id": null,
  "media": null,
  "raw_kind": "conversation"
}
```

Gateway event fields:

- `source`: `whatsapp`
- `source_message_id`: `<account_id>:<remote_jid>:<message_id>`
- `user_id`: `sender_jid`
- `conversation_id`: `whatsapp:<account_id>:<remote_jid>`
- `content`: normalized text plus media placeholders when needed
- `meta.delivery_channel`: `whatsapp`
- `meta.account_id`: account id
- `meta.chat_id`: remote JID
- `meta.sender_jid`: sender JID
- `meta.chat_type`: `dm` or `group`
- `meta.source_message_id`: WhatsApp message id
- `meta.quoted_message_id`: quoted message id when present

Dedup must use `(source, source_message_id)`.

The sidecar should skip:

- `fromMe` messages unless self-chat mode is explicitly enabled;
- protocol messages;
- receipt/status messages;
- broadcast/status JIDs;
- historical append messages older than the connection time minus a short grace
  window.

## Access Control

### DMs

`dm_policy` behavior:

- `disabled`: drop all DMs.
- `allowlist`: accept only `allow_from`.
- `open`: accept anyone not in `blocklist`.
- `pairing`: unknown senders receive a pairing challenge; accepted only after
  operator approval.

For the first implementation, pairing can reuse or adapt the existing Telegram
chat auth pattern:

```text
unknown WhatsApp DM
  -> store pending sender
  -> notify operator on primary channel
  -> operator approves/denies
  -> approval writes allow_from/blocklist
```

Do not auto-write unknown senders into config.

### Groups

Default behavior:

- `group_policy: allowlist`
- `require_group_mention: true`

Group message accepted only if:

1. group JID is in `group_allow_from`;
2. message mentions the assistant or quotes the assistant, unless
   `require_group_mention: false`;
3. sender is not blocklisted.

Group approval can ship after DM support if needed, but group messages must not
be processed by default.

## Outbound Lifecycle

```text
Gateway has assistant response
  -> delivery layer resolves WhatsApp channel
  -> WhatsAppChannel.send(response, meta)
  -> sidecar send command
  -> Baileys sock.sendMessage(remote_jid, payload)
  -> sidecar returns message id
  -> gateway marks delivery complete
```

Sidecar command:

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

Text formatting:

- Convert Markdown tables to plain bullets or code blocks before send.
- Convert common Markdown emphasis to WhatsApp-safe formatting only when it is
  unambiguous.
- Do not attempt rich MarkdownV2 escaping. WhatsApp formatting is different
  from Telegram.
- Chunk very long replies before send. Start with a conservative 3500 character
  limit.

## Media

First milestone:

- inbound image and audio metadata recognized;
- media downloaded to instance state only when `channels.whatsapp.media.enabled`
  is true;
- image events set `meta.image_path` so existing vision routing can apply;
- audio events may be transcribed in a later milestone unless a low-risk bridge
  to existing voice ASR is simple.

State path:

```text
state/channels/whatsapp/media/<account_id>/<message_id>/<filename>
```

Security:

- enforce max bytes before writing;
- generate safe filenames;
- store MIME and SHA256 metadata;
- do not execute or parse documents in the first release.

## Recovery And Watchdog Integration

The new intelligent watchdog should recognize WhatsApp health signals.

Sidecar must surface:

- `logged_out`: credentials invalid or WhatsApp explicitly logged out;
- `reconnecting`: socket closed but not logged out;
- `auth_missing`: no creds exist;
- `send_failed`: outbound send failed;
- `sidecar_crashed`: process exited unexpectedly.

Gateway behavior:

- `logged_out` should notify the operator and mark channel unhealthy.
- Watchdog should mention `jc whatsapp login` as the recovery action.
- If a WhatsApp inbound event cannot be answered because outbound auth expired,
  the gateway should fail the event with a clear channel-auth error instead of
  pretending it delivered.
- If the brain is healthy but WhatsApp outbound is down, do not switch brains;
  this is a channel recovery problem.

## State And Files

Suggested layout:

```text
state/channels/whatsapp/
  auth/<account_id>/creds.json
  auth/<account_id>/creds.json.bak
  chats.jsonl
  pending_senders.json
  media/
  sidecar.log
```

Future multi-account support should keep each account isolated by account id.
Single-account first implementation should still write account ids into state so
the path does not need a breaking migration later.

## Security And Compliance Notes

WhatsApp Web via Baileys is not the official WhatsApp Business API. This matters
for commercial positioning:

- recommend a dedicated assistant number, not the operator's personal number;
- document that Linked Devices can be revoked from the WhatsApp mobile app;
- avoid bulk messaging, scraping, or cold outbound use cases;
- rate-limit outbound sends;
- expose `logout` and auth status clearly;
- leave a future path for official Meta Cloud API support.

OpenClaw is useful prior art, but do not vendor or copy its implementation until
license, attribution, and compatibility are explicitly reviewed.

## Tests

Unit tests:

- sidecar protocol encode/decode and invalid message handling;
- JID normalization;
- DM allowlist/blocklist/pairing policy;
- group allowlist and mention gate;
- outbound target resolution from `meta.chat_id`;
- chunking and WhatsApp text formatting;
- auth state path resolution.

Integration tests with fake sidecar:

- Python channel starts fake sidecar and enqueues inbound message;
- duplicate inbound message is deduped by queue;
- accepted DM routes through gateway and sends outbound response;
- blocked sender is not enqueued;
- group message without mention is ignored;
- send failure marks event failed with useful error;
- logged-out event produces operator-facing recovery log/notification.

Optional real smoke test:

- dedicated WhatsApp test number;
- `jc whatsapp login --print-qr`;
- send DM from allowed test sender;
- receive one assistant reply;
- `jc whatsapp logout`;
- confirm subsequent send fails with auth recovery instructions.

## Rollout Plan

Phase 1, spec and review:

- This PR: spec only.

Phase 2, sidecar skeleton:

- Node sidecar starts, logs in by QR, stores auth, emits connection status.
- No gateway routing yet.

Phase 3, inbound DM path:

- Python channel starts sidecar.
- Allowed DMs enqueue gateway events.
- Text replies send back through WhatsApp.

Phase 4, access control:

- Pairing approval flow.
- Group allowlist and mention gate.
- CLI for sender/chat management.

Phase 5, media and production hardening:

- inbound images;
- optional audio;
- watchdog health integration;
- reconnect/backoff;
- release hook scaffolding.

## Open Questions

- Should the first build require Node in the framework venv setup, or should the
  sidecar be packaged as an npm dependency installed on demand?
- Should pairing approvals use Telegram as the operator channel first, or a
  generic operator notification interface?
- Should WhatsApp be single-account until proven, or should config expose
  `accounts:` immediately?
- Should official Meta Cloud API support be a parallel channel (`whatsapp_cloud`)
  or a backend under the same `whatsapp` channel?
- What is the commercial support statement for Baileys-based WhatsApp Web?

## Review Checklist

- [ ] Agree that JuliusCaesar should use Baileys directly rather than importing
      `@openclaw/whatsapp`.
- [ ] Agree that the first boundary is Python gateway plus Node sidecar over
      stdio JSON lines.
- [ ] Agree on conservative default policies for DMs and groups.
- [ ] Agree that WhatsApp Web is positioned as dedicated-number linked-device
      support, not official Business API support.
- [ ] Agree that watchdog recovery treats WhatsApp auth separately from brain
      health.
