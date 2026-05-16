# Email channel: display-name From, Reply-All, sender normalization

## Goal

Make the JC email channel production-ready for fleet agents that must:

1. Send with RFC 5322 display-name From (`"Daniel Mercer" <daniel.mercer@omnisage.org>`).
2. Default to Reply-All — preserve original Cc list across replies unless instructed otherwise.
3. Preserve thread continuity (already supported via `In-Reply-To` + `References`; no change here, just verify).
4. Accept inbound mail from senders that include a display-name in `From:` (fixes Ethan's
   `_normalize_sender` bug — currently rejects `"Sergio <a@b>"` etc).

Out of scope: HTML bodies, attachments, quoted-body reply formatting, per-thread Reply-All
overrides, S/MIME.

## Motivation

Current state (commit `c76e001`):

- `lib/channels/email/smtp_client.py:98` — `msg["From"] = self.user` (bare address only).
- `lib/channels/email/adapter.py:159` — `send_reply` hardcodes `cc=[]`. Original recipients
  silently dropped.
- `_normalize_sender` is cloned in three places (auth, dispatcher, policy) with an
  `isspace()` check that rejects any RFC 5322 mailbox containing a display-name. Means
  trusted-sender allowlists never match if the sender's mail client adds a display-name.

Daniel Mercer's first email config is the trigger: he needs (1)+(2) day one and would
hit (4) on inbound from any normal external client.

## Changes

### 1 — `from_display_name` in SMTP config

**Config schema** (`lib/gateway/config.py`, email channel block):

```yaml
channels:
  email:
    smtp:
      from_display_name: "Daniel Mercer"   # optional; falls back to bare addr
```

**Plumbing**:

- `EmailChannelAdapter.__init__` reads `smtp_cfg.get("from_display_name")`, stores on self.
- `SMTPClient.__init__` accepts new kwarg `from_display_name: str | None = None`.
- `SMTPClient._build_message`:
  - If `self.from_display_name`: `msg["From"] = email.utils.formataddr((self.from_display_name, self.user))`.
  - Else: unchanged (`msg["From"] = self.user`).

`formataddr` quotes/encodes the display-name per RFC 5322 (handles commas, non-ASCII).

### 2 — Reply-All

**Config schema**:

```yaml
channels:
  email:
    smtp:
      reply_all: true     # default: false (backward compat)
```

**Inbound capture** — already in place. `IMAPClient` already extracts `cc` and `to`
recipients into the `EmailMessage` dataclass (see `imap_client.py:165`,
`adapter.py:117`). The dispatcher meta builder (`email_dispatcher.py:108-110`) currently
copies `in_reply_to` + `references` but not the recipient lists. Add:

```python
meta = {
    ...
    "email_to_recipients": msg.get("to") or [],
    "email_cc": msg.get("cc") or [],
}
```

**Reply path**:

- `EmailChannelAdapter.send_reply` gains `original_to: list[str] | None`,
  `original_cc: list[str] | None` parameters (default `None` = legacy behavior).
- When `self.reply_all and (original_to or original_cc)`:
  - Build CC = `dedupe(original_to + original_cc) - {self.imap_user} - {recipient}`.
  - Drop `recipient` from CC because it's already in To.
  - Case-insensitive dedupe by normalized address (use the new `normalize_sender_addr`).
- Otherwise `cc=[]` (legacy).
- Caller in `gateway.delivery` (or wherever `send_reply` is invoked for email replies)
  reads `email_to_recipients` + `email_cc` from `meta` and passes them through.

### 3 — Single source of truth for sender normalization

**New module** `lib/channels/email/normalize.py`:

```python
from email.utils import parseaddr

def normalize_sender_addr(raw: str | None) -> str | None:
    """Lower-case bare address extracted from any RFC 5322 mailbox or addr-spec.
    Returns None for empty/malformed input."""
    text = str(raw or "").strip()
    if not text:
        return None
    _name, addr = parseaddr(text)
    addr = addr.lower().strip()
    if not addr or "@" not in addr or any(ch.isspace() for ch in addr):
        return None
    return addr
```

**Replace call sites**:

- `lib/channels/email/authorization.py:16` — drop local `_normalize_sender`, import from
  `normalize`.
- `lib/gateway/channels/email_dispatcher.py:60` — same.
- `lib/gateway/channels/email_policy.py:30` — `_normalize_addr` had no `isspace()` guard
  but also no `parseaddr`. Replace with `normalize_sender_addr`. The single equality with
  trusted list now works regardless of whether the inbound `From:` contains a display-name.

### 4 — Tests

New `tests/channels/email/test_normalize.py`:

| input | output |
|---|---|
| `"sergio@scovai.com"` | `"sergio@scovai.com"` |
| `"Sergio Gutierrez <sergio@scovai.com>"` | `"sergio@scovai.com"` |
| `'"S, G" <a@b>'` | `"a@b"` |
| `"a@b (comment)"` | `"a@b"` |
| `""` / `None` / `"no-at"` | `None` |

Update `tests/channels/email/test_authorization.py`: trusted=`["a@b"]`, raw=`"X Y <a@b>"`
→ `"trusted"`.

New `tests/channels/email/test_smtp_from_header.py`:

- `_build_message` with `from_display_name="Daniel Mercer"` and user `daniel.mercer@omnisage.org`
  → `msg["From"]` parses (via `email.utils.getaddresses`) back to that pair.
- Display-name with comma → still single RFC mailbox (quoted by formataddr).
- `from_display_name=None` → bare `msg["From"] = self.user`.

New `tests/channels/email/test_reply_all.py`:

- `send_reply(..., reply_all=True, original_to=[a, b], original_cc=[c])` with self=`a`
  → CC = `[b, c]`, To = `[recipient]`.
- `reply_all=False` → `cc=[]` regardless.
- Self excluded case-insensitively.

`tests/gateway/test_email_dispatch.py` (if exists, else new): inbound message with display-name
sender + CC list → dispatched (not blocked), meta carries `email_to_recipients` + `email_cc`.

### 5 — Daniel instance configuration (post-merge)

Out of scope for the spec itself but unblocked by it. Will land in a follow-up commit
on the instance repo, not in JC framework. Documented here for traceability:

- `.env`:
  ```
  IMAP_HOST=cloudmail.gplugin.com
  IMAP_USER=daniel.mercer@omnisage.org
  IMAP_PASSWORD=<credential — written to .env only, never logged>
  ```
- `ops/gateway.yaml`:
  ```yaml
  # IMMUTABLE. Do not replace or remove the first account. To add a second
  # mailbox, append a new entry — never edit the existing one.
  channels:
    email:
      enabled: true
      imap:
        host: cloudmail.gplugin.com
        port: 993
        mailbox: INBOX
      smtp:
        port: 587
        from_display_name: "Daniel Mercer"
        reply_all: true
        sent_folder: Sent
      senders:
        trusted: []   # TBD with Luca
        external: []
        blocked: []
  ```
- Restart Daniel gateway. Verify in `gateway.log`:
  - `email channel ready (imap=cloudmail.gplugin.com:993, smtp=:587)`
  - First poll cycle: `email poll: 0 new` (or N if backlog).
  - Smoke test: send mail from approved sender → Daniel replies with display-name From,
    Cc preserved on the reply.

## Backward compatibility

- `from_display_name` omitted → bare-address From (current behavior).
- `reply_all` omitted → defaults `false` → `cc=[]` (current behavior).
- `normalize_sender_addr` accepts everything the old `_normalize_sender` accepted and more
  (RFC 5322 mailboxes). Existing trusted lists with bare addresses still work.

## Risks / open questions

- **Quoted body in replies.** Spec excludes inline quoted text (`> original message`).
  Header-based threading (already in place) is RFC-clean and renders in Gmail/Outlook/etc.
  If Luca actually wants visible quoted text, that's a separate change in
  `send_reply` body building.
- **CC dedupe edge case.** Case-insensitive on the local part is non-standard
  (RFC 5321 says local-part is case-sensitive). We dedupe case-insensitively because
  every real-world MTA treats it that way. Documented in code comment.
- **Trusted list bootstrap.** Daniel needs a starter `senders.trusted` list. Awaiting
  Luca's input on who's allowed inbound from day one.

## Commit plan

1. `feat(email): normalize_sender_addr shared util — parseaddr-based`
2. `fix(email): collapse _normalize_sender triplet onto normalize_sender_addr`
3. `feat(email): from_display_name SMTP option`
4. `feat(email): reply_all option in SMTP/adapter; carry original recipients in meta`
5. `test(email): coverage for normalize, From header, reply-all`

All on branch `feat/email-display-name-and-reply-all`. PR or direct merge to `main` after Luca review.
