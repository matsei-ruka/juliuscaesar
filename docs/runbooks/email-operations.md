# Email Operations Runbook

Status: Active
Date: 2026-05-01

## Purpose

This runbook gives an operator a boring, repeatable path for the email channel:
confirm configuration, inspect stuck work, approve or reject outbound drafts,
and roll back without deleting state.

## Daily Health Check

```bash
jc email doctor
jc email doctor --json
```

Healthy output should show:

- `enabled: true`
- `IMAP_HOST`, `IMAP_USER`, and `IMAP_PASSWORD` as `ok`
- pending inbound count near zero
- pending draft count near zero
- a moving `last_uid` when new mail arrives

When credentials are missing, `jc email doctor` exits non-zero and prints which
credential is absent.

## Credential Tests

```bash
jc email test-imap
jc email test-smtp
```

Use `test-imap` when no mail is being fetched. Use `test-smtp` when outbound
draft approval fails before a message id is produced.

## Pending Inbound Messages

Unknown senders are persisted before the UID watermark advances. Inspect them:

```bash
jc email pending list
jc email pending show <uid>
```

Approve or deny a sender's local pending messages:

```bash
jc email pending approve sender@example.com
jc email pending deny sender@example.com
```

`jc chats approve --email sender@example.com` remains the sender-policy command:
it promotes the sender to `channels.email.senders.trusted` and drains pending
messages. `jc email pending approve` is an operational drain command for an
already-decided sender.

## Outbound Drafts

External sender replies are stored as drafts:

```bash
jc email drafts list
jc email drafts show draft_777
jc email drafts edit draft_777 "new body"
jc email drafts approve draft_777
jc email drafts reject draft_777
```

Approving a draft sends through SMTP and marks the draft `sent` with
`sent_message_id` and `sent_timestamp`. Rejecting marks it `rejected` without
sending. `jc email drafts list --all` includes sent and rejected drafts for
forensics.

## Metrics

`jc email doctor` exposes the core lifecycle metrics:

- `last_uid`: current IMAP watermark.
- `pending`: count of unknown-sender inbound messages awaiting a decision.
- `oldest_pending`: age of the oldest pending inbound message with a parsed
  mail timestamp.
- `drafts`: total draft records by state.
- `oldest_pending` under drafts: age of the oldest pending outbound draft.
- `event_counts_recent`: lifecycle event counts from
  `state/channels/email/events.jsonl`.
- `last_event`: newest lifecycle event name and timestamp.

These are intentionally local and file-backed so a broken gateway daemon does
not hide operator state.

## Rollback

To disable email intake without deleting state:

```yaml
channels:
  email:
    enabled: false
```

Then restart the gateway or stop the heartbeat email poller. Pending messages,
drafts, and `last_uid` remain under `state/channels/email/`.

## Failure Playbook

### IMAP Fetch Not Moving

1. Run `jc email doctor`.
2. Run `jc email test-imap`.
3. Inspect `state/channels/email/poll.log`.
4. Confirm `channels.email.enabled` and heartbeat poller configuration.

### Pending Messages Growing

1. Run `jc email pending list`.
2. Decide sender policy with `jc chats approve --email` or
   `jc chats deny --email`.
3. Re-run `jc email doctor`.

### Drafts Aging

1. Run `jc email drafts list`.
2. Inspect each old draft with `jc email drafts show`.
3. Approve, edit, or reject.

### SMTP Approval Fails

1. Run `jc email test-smtp`.
2. Check `IMAP_USER`, `IMAP_PASSWORD`, and `SMTP_PORT`.
3. Retry `jc email drafts approve <draft_id>`.
