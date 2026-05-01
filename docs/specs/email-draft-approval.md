# Email Draft Approval (Phase 4)

## Goal

Extend email channel to support three sender tiers: **trusted** (auto-process), **external** (draft approval loop), **blocked** (discard). Enable human-in-the-loop response review for external contacts before outbound email delivery.

## Sender Tiers

Config in `ops/gateway.yaml` under `channels.email.senders`:

```yaml
channels:
  email:
    senders:
      trusted:           # auto-process as prompts (current "allowed")
        - alice@corp.com
        - bob@partner.com
      external:          # draft approval required
        - contact@client.com
        - vendor@external.biz
      blocklist:         # silent drop
        - spam@x.com
```

Tier priorities:
1. Check `blocklist` first → drop silently
2. Check `trusted` → enqueue as prompt, no approval
3. Check `external` → enqueue as draft, wait for approval
4. Fallback: `unknown` status, pending sender decision (same as Phase 2-3)

## Draft Approval Workflow

```
[External email arrives]
     ↓
[Dispatch: allowed=external → _enqueue_draft()]
     ↓
[Gateway processes prompt → LLM generates response text]
     ↓
[Draft stored: state/channels/email/drafts/<sender>/<uid>.json]
     ↓
[Telegram notification: draft text + 3 actions]
     ├→ jc-chats draft approve <uid>
     ├→ jc-chats draft reject <uid>
     └→ jc-chats draft edit <uid> "edited text"
     ↓
[On approve: SMTP sends reply, draft cleaned up]
[On reject: draft deleted, no email sent]
[On edit: text updated, re-notified for approval]
```

## Storage

Draft directory structure:

```
state/channels/email/drafts/
├── contact@client.com/
│   ├── uid_1.json
│   ├── uid_2.json
│   └── ...
└── vendor@external.biz/
    ├── uid_100.json
    └── ...
```

Draft file schema:

```json
{
  "uid": "1",
  "sender": "contact@client.com",
  "subject": "Re: Project status",
  "message_id": "<msg@client.com>",
  "in_reply_to": "<prev@client.com>",
  "references": ["<root@x.com>", "<prev@x.com>"],
  "draft_text": "Thank you for your inquiry...",
  "draft_timestamp": "2026-04-30T10:30:00Z",
  "edit_count": 0,
  "meta": {
    "channel_id": "uid_1",
    "delivery_channel": "email",
    "email_uid": "1"
  }
}
```

## Configuration Changes

### `ops/gateway.yaml`

Replace `senders.allowed` + `senders.blocklist` with three-tier model:

```yaml
channels:
  email:
    senders:
      trusted: []     # list[str]
      external: []    # list[str]
      blocklist: []   # list[str]
```

Migration: existing `allowed` entries → `trusted` tier. Run at deployment time.

### `EmailChannel.send(response, meta)`

Check sender tier before sending:

```python
def send(self, response, meta):
    email_to = meta.get("email_to")  # sender of incoming msg
    tier = self._sender_tier(email_to)
    
    if tier == "trusted":
        # auto-send, existing behavior
        return self._build_and_send_smtp(response, meta)
    elif tier == "external":
        # persist as draft, notify Telegram, return draft_id
        draft_id = self._enqueue_draft(response, meta)
        self._notify_draft_telegram(draft_id, email_to, response[:200])
        return f"draft:{draft_id}"
    elif tier == "blocklist":
        # silence; shouldn't reach here (filtered at dispatch)
        return "dropped"
    else:
        # tier == "unknown"
        # same as current: pending sender decision
        return "pending"
```

## New CLI Commands

### `jc-chats draft approve <uid>`

Approve draft, send email, clean up.

```bash
jc-chats draft approve uid_1
# Output: approved and sent to contact@client.com; draft cleaned
```

### `jc-chats draft reject <uid>`

Reject draft, discard without sending.

```bash
jc-chats draft reject uid_1
# Output: rejected; no email sent
```

### `jc-chats draft edit <uid> "<new text>"`

Edit draft text, update timestamp, re-notify with confirmation.

```bash
jc-chats draft edit uid_1 "Updated response text..."
# Output: draft updated; retransmitted to Telegram for approval
```

### `jc-chats draft list [--sender contact@client.com]`

List pending drafts, optionally filtered by sender.

```bash
jc-chats draft list
# Output: uid_1 contact@client.com "Re: Project status" (3 hours pending)
```

## Code Changes

### `lib/gateway/channels/email_dispatcher.py`

New functions:

- `_enqueue_draft(instance_dir, response, meta) -> str` — persist draft JSON, return uid
- `_notify_draft_telegram(chat_id, sender, draft_preview) -> str` — send Telegram with approve/reject/edit commands, return message_id
- `_sender_tier(cfg, sender) -> Literal["trusted", "external", "blocklist", "unknown"]` — check tier from config
- `approve_draft(instance_dir, uid) -> tuple[bool, str]` — load draft, send SMTP, clean up, return (success, msg)
- `reject_draft(instance_dir, uid) -> tuple[bool, str]` — load draft, delete, return (success, msg)
- `edit_draft(instance_dir, uid, new_text) -> tuple[bool, str]` — update draft, re-notify Telegram

### `lib/gateway/channels/email.py`

Modify `send()`:
- Call `_sender_tier()` to determine auto-send vs. draft mode
- On `tier="external"`, call `_enqueue_draft()` instead of SMTP
- On `tier="unknown"`, return "pending" (no auto-send or draft)

### `bin/jc-chats`

New subcommand: `draft`

```python
def _draft_approve(instance, uid): ...
def _draft_reject(instance, uid): ...
def _draft_edit(instance, uid, new_text): ...
def _draft_list(instance, sender=None): ...
```

Argument structure:

```bash
jc-chats draft approve <uid>
jc-chats draft reject <uid>
jc-chats draft edit <uid> "<text>"
jc-chats draft list [--sender <addr>]
```

### `lib/gateway/config.py`

Update email channel validator to accept three-tier `senders` schema:

```python
"senders": {
    "type": "object",
    "properties": {
        "trusted": {"type": "array", "items": {"type": "string"}},
        "external": {"type": "array", "items": {"type": "string"}},
        "blocklist": {"type": "array", "items": {"type": "string"}},
    },
}
```

## Tests

### `tests/channels/test_email_draft_approval.py` (new)

- `test_external_sender_enqueues_draft` — inbound from external sender → draft enqueued, not sent
- `test_trusted_sender_auto_sends` — inbound from trusted sender → SMTP sent immediately
- `test_blocked_sender_dropped` — inbound from blocklist → dropped, no draft
- `test_unknown_sender_pending` — inbound from unknown → pending (unchanged)
- `test_draft_approve_sends_email` — draft approve → SMTP sends, draft cleaned
- `test_draft_reject_discards` — draft reject → deleted, no SMTP
- `test_draft_edit_updates_text` — edit updates text, re-notifies Telegram
- `test_draft_list_filters` — list with/without --sender filter
- `test_draft_approve_idempotent` — second approve same uid → no-op
- `test_draft_tier_migration` — old allowed list → trusted tier on load

### `tests/channels/test_email_config.py` (update)

- Add tests for new three-tier validator

## Migration

**One-time operator task** (Phase 4 deployment):

Old config format (Phase 2-3):
```yaml
channels:
  email:
    senders:
      allowed: [alice@x.com, bob@x.com]
      blocklist: [spam@x.com]
```

New config format (Phase 4):
```yaml
channels:
  email:
    senders:
      trusted: [alice@x.com, bob@x.com]
      external: []
      blocklist: [spam@x.com]
```

`jc-chats migrate-email-senders` — one-off command that:
1. Reads old `allowed` → copies to new `trusted`
2. Keeps `blocklist` as-is
3. Initializes `external: []`
4. Writes updated YAML
5. Outputs confirmation

## Open Questions

- **Draft timeout**: How long before auto-reject? (default: none, operator must explicitly reject or approve)
- **Notification limit**: Send Telegram on every edit, or only on first draft + on-edit flag?
- **Edit approval**: Does edited draft auto-approve, or re-notify and re-require approval?

(Recommend: no auto-timeout, notify every edit, re-notify with "re-edited" flag, require explicit approve again.)

## Success Criteria

- ✅ Three-tier sender config loads and enforces correctly
- ✅ External sender messages enqueued as drafts, not sent immediately
- ✅ Draft approve/reject/edit commands work end-to-end
- ✅ Telegram notifications show drafts with action buttons
- ✅ Old config migrates cleanly to new tier model
- ✅ All new tests pass; regression tests pass
- ✅ No breaking changes to trusted/blocklist tiers
