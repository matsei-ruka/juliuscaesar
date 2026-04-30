# Email Channel Specification

**Status:** Design  
**Author:** Claude Code  
**Target:** JuliusCaesar v0.3.0+  
**Scope:** Add email (IMAP/SMTP) as a bidirectional JC channel, replacing the heartbeat-based email-watch pattern with first-class channel support.

## Overview

Email becomes a standard JC channel alongside Telegram, Slack, Discord, and Voice. The channel:
1. Polls IMAP for new messages at configurable intervals
2. Routes messages from approved senders directly to the assistant as prompts
3. Routes messages from unknown senders to Telegram for approval/block/ignore decisions
4. Sends assistant responses back to email senders via SMTP with full threading
5. Prevents prompt injection via HTML stripping, body truncation, and sender sandboxing

## Architecture

### Components

**1. Email Adapter (`lib/channels/email/adapter.py`)**
- Plugs into gateway's channel dispatch like Telegram/Slack
- Implements `ChannelAdapter` protocol: `receive()`, `send()`, `acknowledge()`
- State: IMAP UID watermark stored in `state/channels/email/last_uid`
- Polling: heartbeat task or standalone daemon (configurable; heartbeat preferred for single-instance)

**2. IMAP Client (`lib/channels/email/imap_client.py`)**
- Wraps `imaplib.IMAP4_SSL` with connection pooling
- Methods: `connect()`, `fetch_new(since_uid)`, `fetch_unread()`, `search(criteria)`, `disconnect()`
- Uses `BODY.PEEK` + readonly select (no server-side side effects)
- Handles encoding normalization, multipart extraction, charset fallback

**3. SMTP Client (`lib/channels/email/smtp_client.py`)**
- Wraps `smtplib.SMTP` with STARTTLS
- Methods: `send(msg)`, `archive_to_sent(msg, folder)`
- Automatic copy to IMAP Sent folder (idempotent create if missing)
- Threading headers: `In-Reply-To`, `References`

**4. Sender Authorization (`lib/channels/email/authorization.py`)**
- Reads allowlist/blocklist from `ops/gateway.yaml` + `.env` overrides
- Hot-reload on mtime change (like PR #30)
- Fallback: unknown senders → Telegram notification, await approval

**5. Injection Sanitization (`lib/channels/email/sanitize.py`)**
- HTML→text conversion (strip tags, preserve structure)
- Body truncation: 8000 chars max (configurable; soft limit with "… [truncated]" notice)
- Encoding normalization (UTF-8, replaces invalid bytes)
- Sender wrapper: `[EMAIL from <addr>, subject: "..."]` → body
- No `From:` injection (rewrite with authenticated sender, not user-controlled)

**6. Polling Task (`bin/jc-email-poller`)**
- Optional standalone daemon (for high-frequency polling)
- Default: heartbeat fetch task `heartbeat/fetch/email-poll.sh`
- Connects → fetches new since UID → dispatch to gateway → saves watermark
- Logs: `state/channels/email/poll.log`

### Data Flow

```
[IMAP inbox]
     ↓
[email-poller: fetch_new(last_uid)]
     ↓
[sender in allowlist?]
  ├─ YES → [sanitize body] → [dispatch to gateway as prompt]
  └─ NO  → [notify Telegram: "New from X"] 
           ├─ User approves  → [add to allowlist, hot-reload] → [dispatch]
           ├─ User denies   → [add to blocklist, hot-reload] → [discard]
           └─ User ignores  → [keep pending, retry next poll]
     ↓
[Assistant generates response]
     ↓
[email-send: SMTP → Sent folder]
     ↓
[Telegram notification: "Replied to X"]
```

### Gateway Config Schema

Location: `ops/gateway.yaml`

```yaml
channels:
  email:
    enabled: true
    
    # IMAP polling
    imap:
      host: mail.example.com
      port: 993
      user: ${IMAP_USER}              # from .env
      password: ${IMAP_PASSWORD}      # from .env
      mailbox: INBOX
      poll_interval: 300              # seconds; 0 = disabled
      body_limit: 8000                # chars; truncate if exceeded
    
    # SMTP sending
    smtp:
      host: mail.example.com
      port: 587
      user: ${IMAP_USER}
      password: ${IMAP_PASSWORD}
      sent_folder: Sent               # auto-create if missing
      signature: |                    # appended to outgoing
        --
        Mario Leone
        COO, Omnisage LLC
        mario.leone@scovai.com
    
    # Sender approval
    senders:
      allowed:
        - mario.leone@scovai.com
        - filippo.perta@scovai.com
        - sergio.gutierrez@scovai.com
      
      blocklist:
        - noreply@sendgrid.com        # opt-out of notifications
        - marketing@spam.com
      
      # Unknown senders: notify Telegram?
      notify_on_unknown: true
      telegram_chat_id: 28547271      # or pull from User memory
    
    # State file
    state:
      last_uid_file: state/channels/email/last_uid
      log_file: state/channels/email/poll.log
```

### .env Variables

```bash
IMAP_HOST=mail.example.com
IMAP_PORT=993                    # optional, default 993
IMAP_USER=mario.leone@scovai.com
IMAP_PASSWORD=<app-password>     # NOT account password
SMTP_PORT=587                    # optional, default 587
```

## Request/Response Format

### Incoming Email → Prompt

```json
{
  "channel": "email",
  "channel_id": "uid_<UID>",
  "conversation_id": "email_<sender_addr>",
  "user_id": "email_sender_<addr>",
  "sender": "mario.leone@scovai.com",
  "sender_name": "Mario Leone",
  "subject": "API key provisioned for Sergio",
  "message_id": "<abc123@scovai.com>",
  "headers": {
    "in_reply_to": "<prev@scovai.com>",
    "references": ["<root@scovai.com>", "<prev@scovai.com>"]
  },
  "text": "[EMAIL from mario.leone@scovai.com, subject: \"API key provisioned for Sergio\"]\n\nCiao,\n\nFilippo has authorized the API key generation...\n\n[… full body, 8000 chars max …]"
}
```

### Outgoing Response → Email

```json
{
  "channel": "email",
  "conversation_id": "email_mario.leone@scovai.com",
  "to": ["mario.leone@scovai.com"],
  "cc": [],
  "subject": "Re: API key provisioned for Sergio",
  "headers": {
    "in_reply_to": "<abc123@scovai.com>",
    "references": ["<abc123@scovai.com>"]
  },
  "text": "Ricevuto. Key salvata in vault.\n\n[signature auto-appended]"
}
```

## Security & Injection Prevention

### HTML Stripping

MIME emails often arrive as multipart with text/html. The adapter:
1. Prefers `text/plain` part (if available)
2. Falls back to `text/html` → strip tags (keep text content)
3. Never passes raw HTML to the model
4. Handles `Content-Transfer-Encoding` (base64, quoted-printable)

### Sender Spoofing Prevention

- IMAP `From:` header is server-verified (IMAP server is trusted)
- Assistant response always uses authenticated `IMAP_USER` as `From:`
- No user-controlled `From:` injection possible
- Allowlist is the firewall: unknown senders can't inject prompts

### Body Truncation

- Soft limit: 8000 chars (configurable in `gateway.yaml`)
- Exceeding messages get `[… message truncated, continue in original email …]` notice
- Full body still accessible via `conversation_id` + retry with `--body-offset` (future enhancement)

### Rate Limiting

- Per-sender: max 1 inbound per minute (configurable)
- Burst protection: reject if >5 pending messages from same sender
- Prevents `fork bomb` via email

## Hot-Reload & Approval Workflow

### Adding Sender to Allowlist

Interactive flow via Telegram:
```
[EMAIL from unknown@corp.com, subject "Meeting notes"]

Approve / Deny / Ignore?

👤 User taps [Approve unknown@corp.com]
   ↓
[jc-chats approve --email unknown@corp.com]
   ↓
[ops/gateway.yaml: append to senders.allowed]
   ↓
[gateway reloads config on next mtime check (~5s)]
   ↓
[email queued and now dispatched as prompt]
```

Same pattern as PR #30 (config-only sender approval for Telegram).

### Rejection & Blocklist

```
[User taps [Deny unknown@corp.com]]
   ↓
[jc-chats deny --email unknown@corp.com]
   ↓
[ops/gateway.yaml: append to senders.blocklist]
   ↓
[reload] → future mail from blocklist → silent discard (no notification)
```

## Implementation Phases

### Phase 1: MVP (Core Channel)
- [x] IMAP/SMTP clients (wrap existing email-check.py / email-send.py logic)
- [x] Email adapter + gateway integration
- [x] Allowlist/blocklist config in `gateway.yaml`
- [x] HTML stripping + body truncation
- [x] Threading headers (In-Reply-To, References)
- [x] Heartbeat polling task
- [ ] ~2500 LOC, 95% test coverage

### Phase 2: UX (Approval Workflow)
- [ ] `jc-chats approve/deny --email <addr>` subcommands
- [ ] Hot-reload integration (mtime watch + reload trigger)
- [ ] Telegram notification UI
- [ ] Conversation memory keying (thread continuity)

### Phase 3: Polish (Optional)
- [ ] Per-sender rate limits (YAML config)
- [ ] Body offset + pagination (for long emails)
- [ ] DKIM/SPF validation on inbound
- [ ] Delivery status notifications (bounces)
- [ ] Forwarding rules (CC manager@, etc.)

## Testing

### Unit Tests
- `test_imap_client.py` — connection, fetch, UID handling, encoding
- `test_smtp_client.py` — send, archive, threading headers
- `test_sanitize.py` — HTML strip, truncation, injection attempts
- `test_authorization.py` — allowlist/blocklist logic, hot-reload

### Integration Tests
- Full round-trip: send email → fetch → dispatch → reply → check Sent folder
- Multipart handling (HTML + plain text, attachments)
- Threading continuity (In-Reply-To chains)
- Config reload without dropping in-flight messages

### Security Tests
- Injection attempts: `From:` spoofing, HTML execution, script tags
- Rate limiting: burst detection, backoff
- Blocklist: ensure blocked senders never reach model

## Rollout Plan

1. **Deploy to sergio_dev_ops** — test with Mario's emails (allowed senders only)
2. **Smoke test** — verify threading, Sent folder archive, Telegram notifications
3. **Deploy to FrancescoDatini** — full approval workflow
4. **Deploy to rachel_zane** — (optional; Luca may not use email channel)
5. **Remove heartbeat fetch tasks** — migrate to channel poller

## Open Questions / Future

- Should email channel support **team mailboxes** (shared inboxes)?
  - Current design: 1 mailbox per instance (IMAP_USER)
  - Future: multi-account with sender routing
- **Attachment handling?** Current scope: text only, ignore attachments
  - Phase 3: pass attachment metadata to model, ask about fetch?
- **Reply-to vs From?** Currently always reply to sender's address
  - Variant: forward to team instead of direct reply?

## References

- PR #30: config-only sender approval (authorization pattern)
- `ops/email-check.py`, `ops/email-send.py` (existing Sergio setup)
- `lib/channels/telegram/adapter.py` (gateway integration template)
