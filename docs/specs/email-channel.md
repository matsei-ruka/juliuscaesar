# Email Channel Operating Spec

Status: Design consolidation
Date: 2026-05-01
Branch: `feat/email-channel`

## Goal

Make email a first-class JuliusCaesar gateway channel for corporate operators.
The channel receives mail through IMAP, routes accepted messages through the
gateway, and sends replies through SMTP. Unknown inbound senders and external
outbound replies are handled through explicit operator workflows.

This spec replaces the split `email-channel` and `email-draft-approval` drafts.
It defines one lifecycle from inbox fetch to outbound reply.

## Product promise

Email support is corporate-useful when an operator can:

- connect a mailbox without custom scripting;
- decide who may reach the assistant;
- review outbound replies for external contacts;
- recover from IMAP, SMTP, Telegram, or gateway outages;
- inspect what happened after the fact.

## Non-goals

- Do not build a general email client.
- Do not support arbitrary mailbox delegation in the first release.
- Do not parse or execute attachments in the first release.
- Do not require public OpenAI API keys.
- Do not silently send external corporate email without an explicit policy.

## Sender tiers

Email sender policy lives under `channels.email.senders` in
`ops/gateway.yaml`.

```yaml
channels:
  email:
    senders:
      trusted:
        - alice@corp.com
      external:
        - client@example.com
      blocklist:
        - spam@example.net
```

Resolution order:

1. `blocklist` -> discard and record an ops event.
2. `trusted` -> enqueue inbound message and auto-send the assistant reply.
3. `external` -> enqueue inbound message, but store the outbound reply as a
   draft for operator approval.
4. unknown -> persist pending inbound message, notify the operator, wait for an
   approve/deny decision.

Migration:

- Existing `senders.allowed` entries become `senders.trusted`.
- Existing `senders.blocklist` is preserved.
- `senders.external` starts empty unless the operator configures it.
- The loader may accept `allowed` as a backward-compatible alias, but writers
  should emit only the three-tier format.

## Inbound lifecycle

```text
IMAP fetch
  -> parse + sanitize
  -> persist local record
  -> classify sender tier
  -> trusted/external: enqueue gateway event
  -> unknown: persist pending and notify operator
  -> blocklist: record drop and stop
  -> advance UID watermark only after durable local handling succeeds
```

The UID watermark must never advance merely because a message was fetched. It
advances only after the message is either enqueued, persisted as pending,
recorded as blocked, or recorded as a permanent parse failure. This prevents
unknown-sender messages from disappearing when the operator ignores the first
notification.

Pending inbound messages live in local state and are drained after approval:

```text
state/channels/email/pending/<safe_sender_key>/<message_uid>.json
```

The sender key must be safe and canonical. Use URL-safe base64 of the normalized
email address or a hash prefix plus metadata in the JSON body. Do not use raw
email addresses as path segments.

## Outbound lifecycle

When the gateway produces an assistant response for an email event:

1. Resolve the original sender tier from current config.
2. For `trusted`, send via SMTP immediately.
3. For `external`, persist a draft and notify the operator.
4. For unknown or blocklisted senders, do not send. Record the skipped send.

External sender draft flow:

```text
external inbound email
  -> gateway event
  -> assistant response
  -> draft stored locally
  -> operator notified on Telegram
  -> approve: SMTP send + mark sent
  -> reject: mark rejected, no send
  -> edit: update draft text, require approval again
```

Drafts live in:

```text
state/channels/email/drafts/<safe_sender_key>/<draft_id>.json
```

Draft schema:

```json
{
  "draft_id": "draft_20260501_000001",
  "sender": "client@example.com",
  "sender_key": "Y2xpZW50QGV4YW1wbGUuY29t",
  "subject": "Re: Project status",
  "message_id": "<msg@client.example>",
  "in_reply_to": "<msg@client.example>",
  "references": ["<root@example>", "<msg@client.example>"],
  "draft_text": "Thank you for your inquiry...",
  "draft_timestamp": "2026-05-01T10:30:00Z",
  "edit_count": 0,
  "state": "pending",
  "failure_error": null,
  "failed_timestamp": null,
  "meta": {
    "delivery_channel": "email",
    "email_uid": "123",
    "conversation_id": "email:client@example.com"
  }
}
```

## CLI surface

Prefer a dedicated email command group:

```bash
jc email pending list [--sender <addr>]
jc email pending approve <sender>
jc email pending deny <sender>
jc email senders list [--json]
jc email senders trust <sender>
jc email senders external <sender>
jc email senders block <sender>
jc email drafts list [--sender <addr>]
jc email drafts show <draft_id>
jc email drafts approve <draft_id>
jc email drafts reject <draft_id>
jc email drafts edit <draft_id> <text>
jc email doctor
jc email test-imap
jc email test-smtp
```

Compatibility aliases may remain:

```bash
jc-chats approve --email <addr>
jc-chats deny --email <addr>
```

The alias should remain for compatibility, but new operator guidance should use
the first-class `jc email` commands.

## Configuration

Minimal config:

```yaml
channels:
  email:
    enabled: true
    imap:
      host: ${IMAP_HOST}
      port: 993
      user: ${IMAP_USER}
      password: ${IMAP_PASSWORD}
      mailbox: INBOX
      poll_interval: 300
      body_limit: 8000
    smtp:
      host: ${SMTP_HOST}
      port: 587
      user: ${SMTP_USER}
      password: ${SMTP_PASSWORD}
      sent_folder: Sent
      signature: ""
    senders:
      trusted: []
      external: []
      blocklist: []
    approvals:
      telegram_chat_id: ${TELEGRAM_CHAT_ID}
      notify_on_unknown: true
      notify_on_draft: true
    state:
      last_uid_file: state/channels/email/last_uid
      pending_dir: state/channels/email/pending
      drafts_dir: state/channels/email/drafts
      log_file: state/channels/email/poll.log
```

Environment variables:

```bash
IMAP_HOST=mail.example.com
IMAP_PORT=993
IMAP_USER=assistant@example.com
IMAP_PASSWORD=<app-password>
SMTP_HOST=mail.example.com
SMTP_PORT=587
SMTP_USER=assistant@example.com
SMTP_PASSWORD=<app-password>
```

If SMTP credentials are omitted, fall back to IMAP credentials only when the
host configuration explicitly allows that.

## Message shape

Inbound gateway event:

```json
{
  "source": "email",
  "source_message_id": "uid_123",
  "conversation_id": "email:client@example.com",
  "user_id": "email:client@example.com",
  "content": "[EMAIL from client@example.com, subject: \"Project status\"]\n\n...",
  "meta": {
    "delivery_channel": "email",
    "email_to": "client@example.com",
    "email_subject": "Project status",
    "email_message_id": "<msg@client.example>",
    "email_references": ["<root@example>"],
    "email_uid": "123",
    "sender_tier": "external"
  }
}
```

Outgoing SMTP reply uses:

- authenticated SMTP account as `From`;
- original sender as `To`;
- `Re:` subject normalization;
- `In-Reply-To` and `References` for threading;
- configured signature appended after the assistant response.

## Sanitization and sender confidence

The channel must sanitize content before it reaches the model:

- prefer `text/plain`;
- convert `text/html` to text if needed;
- normalize encodings to UTF-8;
- truncate long bodies with an explicit marker;
- wrap the body with sender and subject context;
- never pass raw HTML or raw headers as instructions.

For the first corporate pilot, sender identity confidence may be policy-based:

- trusted senders are trusted only within a controlled mailbox/provider setup;
- the channel records available authentication headers for audit;
- if provider authentication results are absent or fail, the message should be
  treated as `external` or `unknown`, not `trusted`.

Future hardening can add strict DKIM/SPF/DMARC policy, but the current product
contract is simpler: do not pretend the `From` header alone is verified.

## Observability

Every lifecycle transition should produce a structured log entry:

- fetched
- parsed
- persisted
- enqueued
- pending
- approved
- denied
- drafted
- edited
- sent
- rejected
- failed
- blocked

Counters needed for corporate pilot:

- fetched messages
- pending inbound messages
- approved senders
- denied senders
- drafts pending
- drafts sent
- drafts rejected
- SMTP failures
- IMAP failures
- oldest pending age
- oldest draft age

## Tests

Required tests:

- sender tier resolution, including blocklist precedence;
- old `allowed` config migrates to `trusted`;
- unknown sender persists pending before UID advances;
- approving pending sender drains local pending store into gateway queue;
- denied pending sender is removed without enqueue;
- raw sender addresses are not used as filesystem path segments;
- external sender response creates a draft and does not SMTP-send;
- trusted sender response SMTP-sends immediately;
- draft approve sends once and marks final state;
- draft approve failure marks `failed` and records a lifecycle event;
- draft reject never sends;
- draft edit requires a later approve;
- Telegram notification failure does not lose pending/draft state;
- `jc email doctor` reports missing credentials and stale pending/draft items.
- Company `gateway.snapshot` includes email pending/draft/lifecycle metrics
  when the channel is enabled or local email state exists.

## Rollout

Pilot readiness:

1. Configure one controlled mailbox.
2. Run `jc email test-imap`.
3. Run `jc email test-smtp`.
4. Send a trusted-sender email and verify auto-reply.
5. Send an unknown-sender email and verify pending approval.
6. Promote one external sender and verify draft approval.
7. Confirm logs, `jc email doctor`, and Company snapshot counters match the
   observed flow.

Roll back by setting:

```yaml
channels:
  email:
    enabled: false
```

Disabling the channel must not delete pending messages, drafts, or UID state.

## Definition of done

Email is pilot-ready when:

- the consolidated lifecycle is implemented;
- setup and doctor commands make misconfiguration obvious;
- UID state cannot lose unknown messages;
- external outbound drafts cannot send without approval;
- operator decisions are durable and auditable;
- focused email tests pass;
- existing Telegram gateway tests still pass.
