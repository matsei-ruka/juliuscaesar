# Spec: Unified Local Approval System

**Status:** Draft — pending review
**Date:** 2026-05-14
**Branch:** `spec/unified-approval-system`
**Scope:** Collapse the five disjoint approval lanes in JC today into one organic local subsystem with a single sqlite-backed record, a single producer API, and two channel adapters (Telegram + Email/DKIM). Migrates self_model, dream, user_model, sender approval, and telegram-group approval onto the same table. The HTTP-based `lib/company/approvals.py` remains as a thin transport that can optionally write into the unified table for audit; otherwise it stays untouched.

**Supersedes (kept on disk for trace, linked below):**
- `docs/specs/gateway-sender-approval.md`
- `docs/specs/sender-approval-config-only.md`
- `docs/specs/telegram-group-auth.md`
- Approval phase only of `docs/specs/dreaming-and-self-improve.md` §6 (rest stays canonical)

---

## 0. Why this exists

JC currently runs five independent approval lanes, none of which share a state machine, a storage layer, or a notification surface:

| # | Producer | Storage today | Channel | Decision gate |
|---|---|---|---|---|
| 1 | `lib/self_model/applier.py` | `memory/staging/proposals-*.jsonl` (`lib/self_model/store.py`) | DKIM email (stub at `applier.py:65-74` — returns `False` always) | `_verify_dkim_approval(instance_dir, proposal_id)` |
| 2 | `lib/dream/apply.py` | `state/dreams/{pending,retained,approved,rejected}/<diff_id>.json` | CLI only (`jc-dream approve <diff_id>`) | filesystem rename |
| 3 | `lib/user_model/cli.py` | `memory/staging/user-model-{staging,applied,rejected}.jsonl` (`lib/user_model/store.py:35-55`) | CLI only (`jc-user-model apply <id>`) | filesystem rename |
| 4 | `lib/gateway/channels/telegram.py` (sender approval) | `ops/gateway.yaml` `channels.telegram.{chat_ids,blocked_chat_ids}` + `.env` `TELEGRAM_CHAT_IDS` | inline `chat_auth:<allow\|deny>:<chat_id>` callback | operator id check (`telegram.py:425-433`) |
| 5 | `lib/gateway/channels/telegram.py` `_handle_bot_added` (group join) | same as #4 | same as #4 | same as #4 |
| 6 | `lib/gateway/channels/email_dispatcher.py` (email draft approval) | `state/email/drafts/*.json` (out of unified scope today; see §3) | inline `jcemail:<approve\|reject>:<draft_id>` | operator id check |
| 7 | `lib/company/approvals.py` | remote HTTP service | external transport | server-side |

Symptoms of fragmentation:
- The `self_model` DKIM gate has been a `return False` stub since 2026-03 (`lib/self_model/applier.py:74`). Nothing applies RULES/IDENTITY diffs except a manual file edit. The "approval channel" is dead code.
- Principal email is **hardcoded** as `filippo.perta@scovai.com` in `lib/self_model/applier.py:47` and `lib/self_model/cli.py:121`. Every instance (Rachel, Marco, Harold, Anika, Elliot, …) shares this string. Wrong for every instance whose principal isn't Filippo (i.e. every instance).
- Sender approval state is split across `ops/gateway.yaml` and `.env` (see superseded spec); group approval shares the surface but is invoked from a different code path. Idempotency / re-prompt is "best effort" via an in-process set (`telegram.py:_auth_prompts_sent`).
- No common operator view. There is no `jc approvals list` answering "what's pending across the instance right now?"
- No common audit trail. `state/dreams/<utc>.md` logs dreams; `memory/staging/*.jsonl` logs proposals; `ops/gateway.yaml` mutations show in git. Three disjoint audit surfaces.
- Idempotent decide-callback semantics exist only on the HTTP `company` lane. Locally, double-clicking Allow can rewrite the yaml file twice; tapping Approve after rejection has no defined behavior.

This spec defines one table, one Python entrypoint, one CLI, one Telegram card format, one Email-DKIM gate. Existing producers migrate; transport-only consumers (the remote `company` service) keep their wire format and gain an optional local write-through.

---

## 1. Glossary

| Term | Meaning |
|---|---|
| **Approval** | A single decision record in the unified table. One row, one decision, one outcome. Identified by `approval_id` (uuid-v4 hex). |
| **Kind** | Discriminator for the payload schema. Enum: `self_model_diff`, `dream_diff`, `user_model_diff`, `sender_authorize`, `group_authorize`, `email_draft`, `action`, `image`, `message`. Closed set; new kinds require a spec amendment. |
| **Producer** | Code that creates approvals: `lib/self_model/applier.py`, `lib/dream/apply.py`, `lib/user_model/applier.py`, `lib/gateway/channels/telegram.py`, `lib/gateway/channels/email_dispatcher.py`, `lib/company/approvals.py` (shim). |
| **Principal** | The single human authorized to decide approvals for this instance. Identified by Telegram user id (numeric) and verified email address. Resolved from gateway conf / env per §4. Never hardcoded. |
| **Main chat** | The Telegram chat id where the bot delivers approval cards to the principal. Equals the principal's DM chat id (`chat_id == user_id` for private chats). Resolved per §3. |
| **Callback token** | High-entropy hex (32 bytes / 64 chars) generated at approval creation. Required on every external decide path. Prevents replay and forgery if the table or a callback URL leaks. |
| **DKIM** | Domain-Keys Identified Mail. Inbound email verification: the receiving MTA validates the `DKIM-Signature` header against the sender domain's published public key. JC trusts a decide-by-email only if DKIM passes for the principal's domain. |
| **Idempotency** | Re-applying the same decide call (same `approval_id` + `callback_token` + action) is a no-op after first commit. The table records the first decision; later attempts return the original outcome with status `already_decided`. |
| **Lifecycle** | `pending → approved \| rejected \| expired`. Terminal states are immutable. No undelete. |
| **Notifier** | Channel adapter responsible for delivering a pending approval to the principal: `lib/approvals/notify/telegram.py`, `lib/approvals/notify/email.py`. Pure send; decoupled from the decide path. |
| **Decider** | Channel adapter responsible for receiving a decision and writing it back: `lib/approvals/decide/telegram.py` (inline keyboard callbacks), `lib/approvals/decide/email.py` (DKIM-verified inbound reply), `lib/approvals/decide/cli.py` (`jc approvals approve <id>`). |

---

## 2. Out of scope

- **Remote `lib/company/` HTTP service.** The wire protocol (`raise_approval` / `wait_for_decision` / `decide`) stays as is. The endpoint, payload, and CompanyClient are not rewritten. An optional local write-through (`company.approvals.raise_approval` → also write a row into the unified table with `kind="action"`) is added; the remote remains source of truth for `company`-originated approvals. This keeps the two-pizza split clean and avoids forcing instances without a `company` endpoint to talk to a remote.
- **Webhook / push receivers.** Decisions land via inline-keyboard taps or DKIM email; we do not open an HTTP endpoint on the instance for this. (`lib/company/` already has its own server-side endpoint; we don't add a second.)
- **Approval scheduling** ("apply this diff at 03:00"). Decisions take effect on the next applier tick or immediately; deferred enactment is a separate spec.
- **Multi-principal voting / quorum approvals.** One principal per instance; if Luca wants Filippo to co-sign something, that's a future spec.
- **Web UI / dashboard.** CLI + Telegram card + email body, nothing else.
- **Per-section frozen-guard rewrite.** `lib/self_model/frozen_sections.py` stays authoritative. The unified system enforces it at decide-time as a re-check; we do not relax the invariant.
- **Backfilling approval rows for historical decisions.** Past mutations in git are the audit trail; the table starts empty.

---

## 3. Architecture

```
                ┌───────────────────────────────────────────────────────┐
                │ Producer (self_model / dream / user_model / sender /  │
                │  group / email_dispatcher / company shim)             │
                │   approvals.raise(kind=..., title=..., payload=...,   │
                │                   expires_in=..., callback=...)       │
                └────────────────────────────┬──────────────────────────┘
                                             │
                                             ▼
                ┌───────────────────────────────────────────────────────┐
                │ lib/approvals/store.py — sqlite                       │
                │   state/approvals.db (single table, see §5)           │
                │   INSERT (status='pending', callback_token, exp_at)   │
                └────────────────────────────┬──────────────────────────┘
                                             │
                              ┌──────────────┼──────────────┐
                              ▼                              ▼
       ┌────────────────────────────────┐    ┌─────────────────────────────────┐
       │ lib/approvals/notify/telegram  │    │ lib/approvals/notify/email      │
       │  → sendMessage(main_chat_id)   │    │  → SMTP to principal_email      │
       │  inline keyboard: approve/rej  │    │  Body includes approval_id +    │
       │  callback_data:                │    │  callback_token + reply token   │
       │   apv:<id>:<approve|reject>    │    │                                 │
       └──────────────┬─────────────────┘    └──────────────┬──────────────────┘
                      │                                     │
                      ▼                                     ▼
       ┌────────────────────────────────┐    ┌─────────────────────────────────┐
       │ lib/approvals/decide/telegram  │    │ lib/approvals/decide/email      │
       │  callback_query handler        │    │  poll INBOX; match Message-ID;  │
       │  validates from_user == princ  │    │  parse reply token; DKIM check  │
       │  → store.decide(id, action,    │    │  → store.decide(id, action,     │
       │     channel='telegram',        │    │     channel='email',            │
       │     decided_by=user_id,        │    │     decided_by=sender_email,    │
       │     callback_token=...)        │    │     callback_token=...)         │
       └──────────────┬─────────────────┘    └──────────────┬──────────────────┘
                      │                                     │
                      └──────────────┬──────────────────────┘
                                     ▼
                ┌───────────────────────────────────────────────────────┐
                │ lib/approvals/dispatch.py — applier callback          │
                │   reads callback_kind from payload, dispatches:       │
                │     self_model_diff → self_model.applier.apply()      │
                │     dream_diff      → dream.apply._write_artifact()   │
                │     user_model_diff → user_model.applier.apply()      │
                │     sender_authorize → config_writer.allow_chat()     │
                │     group_authorize  → config_writer.allow_chat() +   │
                │                        (or block_chat + leaveChat)    │
                │     email_draft      → email_dispatcher.commit_draft  │
                │     action / image / message → producer-defined cb    │
                │   wraps in transaction; idempotent on (id, action)    │
                └───────────────────────────────────────────────────────┘
```

Pure separation: producer writes; notifiers fan out; deciders write back; dispatch runs the side effect. Each arrow is an atomic step. A failure mid-dispatch leaves the row in `approved` state with a non-null `applied_at` only after the applier succeeds; otherwise `applied_at` stays null and a retry can re-run (the applier itself must be idempotent or a no-op on second run).

---

## 4. Main chat resolution

The current resolver lives at `lib/gateway/channels/telegram.py:142-161` (`_main_chat_id`). It is correct for sender approval but uses an implicit fallback to `cfg.chat_ids[0]` which can resolve to a group rather than the principal's DM. The unified system needs an unambiguous answer: **which exact chat is the principal's DM, no fallback to a group**.

### 4.1 Resolution order (canonical)

1. **Env `TELEGRAM_CHAT_ID`** (instance `.env`). Existing convention. Stays the primary source.
2. **YAML `principal.telegram_chat_id`** (new optional field in `ops/gateway.yaml`). Operator-set. Survives `.env` rotation. See §4.3 for schema.
3. **Auto-resolved** from `principal.telegram_user_id` via Telegram `getChat` — only if the chat is a `private` type. We never resolve to a `group` / `supergroup` / `channel`. This is a one-shot resolution cached at instance start; the resolved value is written back into `ops/gateway.yaml` for stability.
4. **Hard fail** — no fallback to `cfg.chat_ids[0]`. If 1–3 all fail, the unified approval producer refuses to raise and logs `approvals: main chat unresolved — refusing to enqueue`. Producers can either (a) drop the approval and log, or (b) fall back to email-only delivery if §5 email is configured. The default is (b); operator can flip to (a) via `approvals.notify.require_telegram: true`.

### 4.2 Why no chat-list fallback

The current `_main_chat_id` falls back to the first entry of `channels.telegram.chat_ids` (`telegram.py:155-158`). For sender-approval that's acceptable — the message just needs to reach the operator somehow. For binding decisions (RULES.md edit, principal-email change), delivering to a 200-member supergroup is a security failure: any member sees the approval card, and only Telegram's `from_user` check on the callback prevents a non-principal from tapping. The risk surface is unnecessary. Enforce DM-only.

### 4.3 YAML schema (additive)

```yaml
principal:
  telegram_chat_id: 28547271     # operator DM chat_id; equals user_id for private
  telegram_user_id: 28547271     # fallback for resolution (often same as chat_id)
  email: luca.mattei@bnesim.com  # principal email — see §5
  email_domain: bnesim.com       # DKIM verification scope; default = email's domain
```

### 4.4 Migration path

- `jc-init` writes `principal:` block from interview answers (currently captured under USER.md only).
- `jc-doctor` adds a check: `principal.telegram_chat_id` resolves; chat type is `private`; chat title matches operator handle. Warn on mismatch.
- One-shot migration script `jc approvals migrate-principal` reads `.env` `TELEGRAM_CHAT_ID` + `USER.md` first heading + best-guess email regex and proposes a `principal:` block via `jc-init` write path.

### 4.5 Code references for the current state

- `lib/gateway/channels/telegram.py:142-161` — current `_main_chat_id` resolver.
- `lib/gateway/channels/email_dispatcher.py:262-271` — duplicate resolution for email draft notifications. Should converge on `lib/approvals/principal.py:main_chat_id(instance_dir)`.
- `lib/self_model/conf.py:36` — `notify_chat_id: str | None = None`. Currently unread by `applier.py`. The unified resolver replaces it; the field is dropped from `SelfModelConfig` in the migration.

---

## 5. Principal email resolution

Today: `filippo.perta@scovai.com` is a string literal in two files (`lib/self_model/applier.py:47`, `lib/self_model/cli.py:121`) and the docstring of `_verify_dkim_approval`. It is wrong for every instance.

### 5.1 Resolution order (canonical)

1. **YAML `principal.email`** (new field, §4.3). Primary.
2. **Env `PRINCIPAL_EMAIL`** (instance `.env`). Secondary; useful for instances where `ops/gateway.yaml` is templated and the email is per-deployment.
3. **Hard fail** — no fallback. If the email is not set, the email notifier and DKIM-decider both refuse to operate and log `approvals: principal email unresolved — email channel disabled`. Telegram-only delivery still works.

### 5.2 DKIM domain scope

DKIM verification is scoped by domain:

- Inbound mail purporting to be from `principal.email` is rejected if the `DKIM-Signature` `d=` field doesn't match `principal.email_domain` (default: the domain of `principal.email`).
- The `Authentication-Results` header from our receiving MTA (or our own dkimpy verification — see §10) must report `dkim=pass`.
- For instances where the principal uses a corporate domain we don't control, we trust the receiving MTA's verification. For instances where we run our own MX, we verify in-process via `dkimpy` (already a transitive dep through Postfix-side tools; if not, add it under `lib/approvals/decide/email.py`).

### 5.3 Hardcode removal

The migration removes the literal `filippo.perta@scovai.com` from:
- `lib/self_model/applier.py:47` (error message) → use `principal.email` from resolver.
- `lib/self_model/applier.py:69` (docstring) → docstring describes the resolver, not a specific address.
- `lib/self_model/cli.py:121` (Hint message) → use resolver.
- `bin/jc-self-model` (help text) → use the resolver name.

No other production file references the string. Grep target: zero occurrences after migration.

### 5.4 Code references for the current state

- `lib/self_model/applier.py:65-74` — stub DKIM verifier. Returns `False` unconditionally.
- `lib/self_model/cli.py:115-124` — error path that prints the hardcoded address as a hint.
- `lib/self_model/conf.py:27-29` — `require_dkim_for_{rules,identity,journal}` flags. Repurposed: become `approvals.require_dkim_by_kind` per-kind map (§7).

---

## 6. Data model

### 6.1 Sqlite schema

One file: `state/approvals.db`. One table: `approvals`. One index per query pattern. WAL mode (matches the existing gateway `queue.db` convention).

```sql
CREATE TABLE IF NOT EXISTS approvals (
  approval_id        TEXT PRIMARY KEY,         -- uuid4 hex, 32 chars
  kind               TEXT NOT NULL,            -- enum, see §1
  title              TEXT NOT NULL,            -- one-line operator-facing summary
  body               TEXT NOT NULL DEFAULT '', -- multi-line markdown body for the card
  payload            TEXT NOT NULL,            -- JSON; schema per-kind (§6.2)
  status             TEXT NOT NULL DEFAULT 'pending',
                                               -- pending | approved | rejected | expired
  requested_at       TEXT NOT NULL,            -- iso8601 UTC
  decided_at         TEXT,                     -- iso8601 UTC; null until decided
  decided_by         TEXT,                     -- principal id ('tg:<user_id>' | 'email:<addr>' | 'cli')
  decision_channel   TEXT,                     -- telegram | email | cli | system
  expires_at         TEXT,                     -- iso8601 UTC; null = no auto-expire
  applied_at         TEXT,                     -- iso8601 UTC; null until applier callback finishes
  callback_token     TEXT NOT NULL,            -- 64-char hex; high entropy
  callback_kind      TEXT NOT NULL,            -- dispatch routing key — usually == kind
  callback_payload   TEXT NOT NULL DEFAULT '{}', -- JSON; applier-specific extras
  producer           TEXT NOT NULL,            -- 'self_model' | 'dream' | 'user_model' | etc.
  source_ref         TEXT,                     -- optional pointer back to producer's record
                                               -- e.g. 'self_model:<proposal_id>',
                                               --      'dream:<diff_id>',
                                               --      'sender:<chat_id>'
  notify_telegram    INTEGER NOT NULL DEFAULT 1, -- 0/1; controls per-row Telegram delivery
  notify_email       INTEGER NOT NULL DEFAULT 0, -- 0/1; controls per-row email delivery
  notified_at        TEXT,                     -- iso8601 UTC; first successful notify
  result             TEXT,                     -- iso8601 UTC + JSON result blob (applier return)
  schema_version     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_approvals_status  ON approvals (status, requested_at);
CREATE INDEX IF NOT EXISTS idx_approvals_kind    ON approvals (kind, status);
CREATE INDEX IF NOT EXISTS idx_approvals_source  ON approvals (source_ref);
CREATE INDEX IF NOT EXISTS idx_approvals_expires ON approvals (expires_at)
  WHERE status = 'pending';
```

Decisions:
- `approval_id` is the operator-facing identifier. 32-char hex (uuid4 stripped). Short enough to fit in a callback_data string (Telegram limits callback_data to 64 bytes — `apv:<32>:approve` = 41 bytes, fine).
- `callback_token` is 64-char hex (256 bits). Never displayed in the Telegram card directly; included only in the email decision path (§9.2) and in the company HTTP shim. Two-secret model: knowing `approval_id` alone cannot decide; the decider needs the token (email path) or to be the in-process callback handler (telegram path, which uses `from_user.id == principal_id` instead of the token).
- `payload` and `callback_payload` are JSON strings, not BLOBs, so they show up in `sqlite3 .dump` and can be reviewed manually.
- `result` mirrors the convention `state/dreams/` uses; on apply, we record the applier's return blob.

### 6.2 Payload contract per kind

Each `kind` has a stable JSON shape in `payload`. Validated by `lib/approvals/schema.py` on `raise()`. Mismatch = `ApprovalSchemaError`.

| Kind | `payload` keys (required) | `callback_payload` keys |
|---|---|---|
| `self_model_diff` | `proposal_id`, `target_file`, `target_section`, `diff` (unified diff text), `risk_class` | `proposal_id` |
| `dream_diff` | `diff_id`, `artifact_path`, `artifact_kind`, `content_excerpt`, `risk_class` | `diff_id` |
| `user_model_diff` | `proposal_id`, `target_file`, `target_section`, `diff`, `risk_class` | `proposal_id` |
| `sender_authorize` | `chat_id`, `chat_type`, `title`, `username`, `message_preview`, `member_count` | `chat_id`, `decision_writes_yaml` (bool) |
| `group_authorize` | `chat_id`, `chat_type`, `title`, `added_by`, `member_count` | `chat_id`, `leave_on_reject` (bool, default true) |
| `email_draft` | `draft_id`, `to`, `subject`, `body_excerpt`, `risk_class` | `draft_id` |
| `action` | `description`, optional `media_refs`, `producer_args` | producer-defined |
| `image` | `description`, `media_path` or `media_url`, `producer_args` | producer-defined |
| `message` | `to`, `channel`, `body_excerpt`, `producer_args` | producer-defined |

The last three (`action` / `image` / `message`) mirror `lib/company/approvals.py:18` semantics exactly, so the company shim can write through with zero translation.

### 6.3 Retention

`state/approvals.db` is append-only in spirit. Rows are not deleted; terminal rows (status ≠ pending) are retained for at least 90 days. A heartbeat builtin `approvals_gc` (disabled by default, opt-in cron once/day) hard-deletes rows where:

- `status in ('approved','rejected','expired')` AND
- `decided_at < now - 90d`.

`approvals_gc --dry-run` lists candidates. No row is hard-deleted while any producer references it (lookup by `source_ref`). The 90-day window is configurable in `ops/approvals.yaml` (`retention_days`).

### 6.4 Migration

New DB file. No schema migration from existing `chats` / `proposals.jsonl` / dream `pending/*.json` — those stay in place during the parallel-run period (§14) and are removed afterwards. Producers that currently write into those locations switch to writing through the `lib/approvals` API; the old stores are read-only during migration and deleted (or moved to `state/.archive/`) at the end of phase D.

---

## 7. Producer API

```python
# lib/approvals/__init__.py
from .store import ApprovalRecord, ApprovalStatus
from .service import raise_, wait_for, decide, get, list_, expire

# Convenience: most callers want raise_+wait_for or raise_+forget.

def raise_(
    instance_dir: Path,
    *,
    kind: str,                          # see §1 enum
    title: str,                         # one line; required
    body: str = "",                     # markdown body for the card
    payload: dict[str, Any],            # per-kind schema, §6.2
    callback_kind: str | None = None,   # defaults to `kind`
    callback_payload: dict[str, Any] | None = None,
    producer: str,                      # required; module name of caller
    source_ref: str | None = None,      # back-pointer to producer record
    expires_in: timedelta | None = timedelta(hours=72),  # default 72h
    notify_telegram: bool = True,
    notify_email: bool | None = None,   # None → use ops/approvals.yaml per-kind default
    media_paths: tuple[str, ...] = (),  # optional attachments (Telegram photo, email attach)
) -> ApprovalRecord:
    """Create a pending approval. Idempotent on (producer, source_ref)
    when both are non-null: a second raise_ returns the existing record.
    Notifies via configured channels asynchronously (does not block on send).
    """


def wait_for(
    instance_dir: Path,
    approval_id: str,
    *,
    timeout: timedelta = timedelta(minutes=10),
    poll_chunk: timedelta = timedelta(seconds=30),
) -> ApprovalRecord:
    """Block until decided or timeout. Same long-poll semantics as
    lib/company/approvals.py:wait_for_decision.  Returns the final
    ApprovalRecord (status approved | rejected | expired | pending).
    """


def decide(
    instance_dir: Path,
    approval_id: str,
    *,
    action: str,                       # 'approve' | 'reject'
    decided_by: str,                   # 'tg:<user_id>' | 'email:<addr>' | 'cli'
    decision_channel: str,             # 'telegram' | 'email' | 'cli'
    callback_token: str | None = None, # required for email/cli; ignored for telegram
    note: str | None = None,           # optional operator note
) -> ApprovalRecord:
    """Idempotent. Second call with same (id, action) is a no-op and
    returns the existing record. Second call with conflicting action
    raises ApprovalConflict.  Triggers the applier callback via
    lib/approvals/dispatch.py once status flips to approved.
    """
```

### 7.1 Backward-compat shim for `lib/company/approvals.py`

`lib/company/approvals.py:raise_approval` and `wait_for_decision` keep their signature. Internally they now:

1. Call `lib.approvals.raise_(...)` to write a local row (best-effort; logged but not blocking if it fails).
2. Continue to POST to the remote `company` service.
3. Return the same `{approval_id, callback_token}` dict.

The local row's `approval_id` is **distinct** from the remote service's `approval_id` (keys differ). The local `source_ref` carries `company:<remote_approval_id>` so they're linkable. Decisions from the remote service callback are mirrored into the local table via a small `company.reporter` hook (`lib/company/reporter.py` — already extant).

This shim is cheap to keep because it removes zero callers; instances that don't use `company` get no new behavior.

### 7.2 Idempotency contract

- `raise_(producer, source_ref)` where both are non-null is idempotent. Re-call returns existing row regardless of status.
- `decide(id, action)` is idempotent on `(id, action)`. Re-calling with the same action returns the existing record; conflicting action on a terminal row raises.
- Expiry is computed at `decide` time and at every `list_(status='pending')` call. The `approvals_gc` builtin sweeps expired pendings on a cron tick. We do not rely solely on a background sweeper — callers see expired status the next time they touch the row.

### 7.3 Self-approval prevention

The agent itself must never tap "approve" on its own pending approvals. Guards:

- `decide()` rejects `decided_by` values that don't match the principal's verified id (`principal.telegram_user_id` or `principal.email`). The CLI surface (`decided_by='cli'`) is only valid when the CLI runs under the operator's shell. We do not allow `decided_by='agent:<instance_id>'`.
- The Telegram decide path enforces `cq.from_user.id == principal.telegram_user_id`. Existing check at `telegram.py:425-432` is the model; lifted into `lib/approvals/decide/telegram.py`.
- The Email decide path enforces DKIM-pass against `principal.email_domain` AND the `From:` header local-part matches `principal.email`.

A future "two-person rule" extension (e.g. a second-principal for IDENTITY changes) is left to a follow-up spec.

---

## 8. Telegram notifier + decider

### 8.1 Card format

A pending approval renders as a single `sendMessage` with an inline keyboard. MarkdownV2 (per RULES). One card per approval. We do NOT batch multiple approvals into one card — operator wants one-tap-per-decision.

```
🟡 *Approval pending* — <kind>

*<title>*

<body — up to 800 chars; truncated with "…" + 'jc approvals show <id>'>

`<approval_id_short>`  ·  expires in <H>h

[ ✅ Approve ]  [ ❌ Reject ]
```

`approval_id_short` is the first 8 hex chars (mirrors `git log --oneline`). Full id appears in the callback_data so we don't need a DB lookup at tap time.

Callback_data format: `apv:<approval_id>:<approve|reject>`.

Why this format (vs. reply-to-approve):

- Inline keyboard is one tap on mobile. Reply-to-approve requires switching to the keyboard and typing a token.
- Telegram delivers `callback_query` with `from.id`, which is exactly the field we need to check against `principal.telegram_user_id` — no extra parsing.
- Reply-to-approve has worse audit story: the bot would need to scan inbound messages for a token, racing with normal user messages.
- The card stays editable post-decision (`editMessageText` adds the outcome: `✅ Approved by you at 14:32 UTC`), so the audit lives in the chat thread itself.

Trade-off accepted: callback_data is capped at 64 bytes. With `apv:` (4) + `:` (1) + `approve` (7) + 32-char id = 44 bytes. Headroom.

### 8.2 Implementation routing

- `_handle_callback_query` (`telegram.py:413`) already dispatches on prefix. Add `apv:` alongside `chat_auth:` (legacy, migrated — see §11.4) and `jcemail:` (legacy, migrated — see §11.5).
- Move the actual approve/reject logic into `lib/approvals/decide/telegram.py`. `telegram.py` only routes.
- Edit message post-decision via `editMessageText` to clear the keyboard and stamp the outcome. Same surface used today for `chat_auth:allow`.

### 8.3 Notification delivery

`lib/approvals/notify/telegram.py:send_card(record)`:

1. Resolve `main_chat_id` via §4.
2. Render card body (MarkdownV2 via the existing `lib/gateway/format/escaper.py`).
3. `sendMessage` with inline keyboard.
4. On 200 OK, record `notified_at` and `message_id` (stored in `callback_payload.tg_message_id` for later `editMessageText`).
5. On non-200, queue a retry on the gateway's existing outbox channel (the dispatcher already retries `sendMessage` calls on failure).

We do not block the producer on the send. `raise_()` returns immediately; notify runs on a background worker (a new tiny `lib/approvals/notify/runner.py` started from the gateway runtime, the same lifecycle hook used by channel adapters).

---

## 9. Email notifier + decider

### 9.1 Email card format

```
Subject: [JC approval] <kind>: <title>

Hi <principal first name>,

A pending approval needs your decision.

  Kind:        <kind>
  Title:       <title>
  Approval id: <approval_id>
  Requested:   <iso8601>
  Expires:     <iso8601>

<body — markdown, full version; no truncation>

To approve, reply to this email with the single line:

  APPROVE <approval_id> <callback_token>

To reject:

  REJECT <approval_id> <callback_token>

For audit, the reply must be DKIM-signed by <principal.email_domain>.

— JC <instance_name>
```

### 9.2 Decide path

`lib/approvals/decide/email.py:run_once(instance_dir)`:

1. Poll INBOX (reuses the existing email channel poller; `lib/channels/email/` ingest).
2. For each unread message:
   - Header `From:` local-part must equal `principal.email`.
   - DKIM verification must pass (`Authentication-Results: dkim=pass` from receiving MTA, OR in-process `dkimpy.verify`).
   - Body's first non-empty line must match the regex `^(APPROVE|REJECT)\s+([0-9a-f]{32})\s+([0-9a-f]{64})\s*$`.
   - The `(approval_id, callback_token)` pair must exist in `state/approvals.db` and the row must be `pending`.
3. On success, call `approvals.decide(approval_id, action='approve'|'reject', decided_by=f'email:{from_addr}', decision_channel='email', callback_token=<token>)`.
4. On failure (DKIM fail, regex mismatch, token mismatch), the email is moved to `.junk-approvals/` (or whatever the channel-email convention is) and logged.

### 9.3 Idempotency on email

Email is the path where replay matters most. Two safeguards:

- The row's `(approval_id, callback_token)` pair is single-use per side effect. After the first successful decide, the row's `decided_at` and `decision_channel` are set; a later replay enters the `already_decided` no-op branch.
- DKIM verification happens **before** lookup. A replayed message with valid DKIM still doesn't move the row's state if it's already terminal.

### 9.4 What the email decider does NOT do

- Does not parse free-form prose. Operators must reply with exactly the `APPROVE`/`REJECT` line.
- Does not support multiple decisions per email. One approval per message.
- Does not auto-render the original card markdown into HTML; plain text body only. (HTML body is a future enhancement.)

---

## 10. State machine

```
       raise_()                       decide(approve)
        ─────▶ pending  ──────────────────────▶ approved
                  │                                 │
                  │ decide(reject)                  │ dispatch.apply()
                  ▼                                 ▼
              rejected                         applied_at set
                  │                                 │
                  │ time > expires_at               │
                  ▼                                 │
              expired                               │
                                                    │
        all states above are terminal except pending
```

Rules:
- Only `pending → {approved, rejected, expired}` is allowed.
- `applied_at` is a separate column; it's set after `dispatch.apply()` succeeds, atomic with the row update. If `dispatch.apply()` raises, `applied_at` stays null, `decided_at` is still set, and the row sits in `approved` state with `applied_at IS NULL`. An operator can re-run via `jc approvals apply <id>`.
- Re-deciding a terminal row returns the existing record with `status='already_decided'` in the return blob (the DB row's `status` is unchanged).
- Conflicting re-decide (approve after reject, etc.) raises `ApprovalConflict` and returns HTTP-409-equivalent at the CLI surface (exit code 9).

### 10.1 Auto-expire policy

Default: 72h from `requested_at` for all kinds. Per-kind override in `ops/approvals.yaml`:

```yaml
expires_in_hours:
  default: 72
  sender_authorize: 168   # 7 days — operator may not see chat-add prompts immediately
  group_authorize:  168
  self_model_diff:  336   # 14 days — diffs are not urgent
  email_draft:      24    # drafts go stale fast
```

Expiry is enforced at `decide` time AND by the `approvals_gc` heartbeat builtin.

### 10.2 Race handling

Both Telegram and Email deciders could fire near-simultaneously. The store layer's `decide()` is wrapped in a `BEGIN IMMEDIATE` transaction that:

1. Re-reads the row.
2. Verifies `status == 'pending'` and `callback_token` matches (when supplied).
3. UPDATEs to terminal state with `decided_at`, `decided_by`, `decision_channel`.
4. COMMITs.

SQLite serializes writes, so the second writer sees `status != 'pending'` and returns `already_decided`. No application-level locking required.

---

## 11. CLI surface

Top-level binary: `jc approvals` (subcommand under existing `jc`). Mirrors the dream / self-model CLI tone.

```
jc approvals list [--status=pending] [--kind=<k>] [--limit=20] [--json]
jc approvals show <id> [--json]
jc approvals approve <id> [--note "..."]
jc approvals reject  <id> [--note "..."]
jc approvals expire  <id>                            # admin override
jc approvals apply   <id>                            # rerun dispatch if applied_at IS NULL
jc approvals gc      [--dry-run] [--days=90]
```

### 11.1 Output shape

Plain text by default:

```
$ jc approvals list
ID        KIND               STATUS    AGE     TITLE
ab83f7   self_model_diff     pending   2h15m   RULES.md §24 — add tone clause
9c1188   sender_authorize    pending   34m     @some_user (private)
7e0021   dream_diff          approved  8h      Auto-merge playbooks/onboarding
```

`--json` prints one record per line as JSON; suitable for `jq` / scripts.

### 11.2 Notification format

The CLI does not notify on `approve`/`reject` (only the producer's applier callback runs). It prints the applier's result blob on success or the error on failure.

### 11.3 Compatibility shims

The existing kind-specific CLI surfaces stay as thin aliases:

- `jc self-model approve <proposal_id>` → looks up `source_ref='self_model:<proposal_id>'`, calls `jc approvals approve <approval_id>`.
- `jc user-model apply <proposal_id>` → same lookup pattern.
- `jc dream approve <diff_id>` → same.
- `jc chats approve <chat_id>` → looks up the pending `sender_authorize` / `group_authorize` row by `source_ref='sender:<chat_id>'` or `'group:<chat_id>'`.

These aliases are deprecated but kept for the same release cycle as `lib/self_model/`'s shim policy (§10 of `dreaming-and-self-improve.md`). Dropped no earlier than two releases out.

---

## 12. Migration plan

One PR per consumer; landed in this order. Each preserves prior behavior until the migration commit flips the producer.

### 12.1 self_model

- Producer: `lib/self_model/applier.py:apply_proposal` no longer reads `_verify_dkim_approval`. Instead it calls `approvals.raise_(kind='self_model_diff', ...)`. The applier-callback (`lib/approvals/dispatch.py`) re-runs the existing apply body on `approved`, skipping the now-dead DKIM check.
- Storage migration: existing `memory/staging/proposals-*.jsonl` entries with `state='staging'` get a one-shot import via `jc approvals migrate self-model` — each row becomes a pending approval row with `source_ref='self_model:<proposal_id>'` and `expires_in=336h`. Original JSONL stays as audit trail.
- CLI: `jc self-model approve` becomes the alias in §11.3.
- Tests to update: `tests/self_model/test_applier.py` (currently fails for non-JOURNAL because of the stub DKIM check). Drop the `_verify_dkim_approval` patch usage; assert the applier raises until a unified approval row flips to approved.

### 12.2 dream

- Producer: `lib/dream/apply.py:apply_artifacts` SENSITIVE branch swaps `_stage(...)` for `approvals.raise_(kind='dream_diff', source_ref=f'dream:{diff_id}', ...)`. The retained-rollback path (`_retain`) is unchanged — it's a separate audit surface for auto-applied artifacts.
- CLI: `jc-dream approve <diff_id>` becomes alias. `jc-dream pending` reads from `state/approvals.db` (filtered to `kind='dream_diff'`) instead of `state/dreams/pending/*.json`.
- Existing `state/dreams/pending/*.json` files are imported by `jc approvals migrate dream`.

### 12.3 user_model

- Producer: `lib/user_model/cli.py:cmd_apply` no longer does the JSONL `move_proposal` directly. It calls `approvals.raise_` if the proposal is not yet in the table, then `approvals.decide` on approve. The JSONL `staging` file becomes the producer-side record (queue of generated proposals); the unified table is the decision record.
- This is the largest behavioral change: today `jc user-model apply` is a single command. After migration, it becomes "raise approval + decide approve in one step" — equivalent to `jc approvals raise --as-user-model ... && jc approvals approve <id>`. The convenience CLI wraps both.
- Tests in `tests/user_model/` get updated assertions; the proposal lifecycle becomes raise → decide.

### 12.4 sender approval

- Producer: `lib/gateway/channels/telegram.py:_maybe_send_sender_approval_prompt` no longer calls `_send_auth_prompt` directly. It calls `approvals.raise_(kind='sender_authorize', source_ref=f'sender:{chat_id}', notify_telegram=True, expires_in_hours=168)`. The unified notifier renders the same card.
- Decision flow: `_handle_callback_query` keeps the routing prefix dispatch but for `apv:` it calls `approvals.decide`. The dispatcher (`lib/approvals/dispatch.py`) calls `lib/gateway/config_writer.py:allow_chat(chat_id)` or `block_chat(chat_id)` exactly as today.
- Behavioral preservation: the sender-approval config-only spec semantics (write to `ops/gateway.yaml` + `.env` atomically; blocklist wins; hot-reload) are unchanged. We only swap the storage of the decision intent from "in-process set + yaml" to "unified table → yaml on decide".
- The legacy `chat_auth:` callback prefix is kept for one release for in-flight cards; new cards use `apv:`.

### 12.5 group approval

- Same as 12.4 but `kind='group_authorize'`. The `leaveChat` call on reject moves from inline in `_block_chat` to the dispatcher's apply-callback (`lib/approvals/dispatch.py:apply_group_authorize`).

### 12.6 email draft approval

- Producer: `lib/gateway/channels/email_dispatcher.py:_send_draft_with_buttons` swaps for `approvals.raise_(kind='email_draft', source_ref=f'email_draft:{draft_id}', ...)`.
- Decider: `jcemail:` callback prefix kept for one release; new cards use `apv:`.
- Dispatcher's `apply_email_draft` calls the existing `commit_draft` / `discard_draft` functions in `email_dispatcher.py`.

### 12.7 company HTTP shim

- `lib/company/approvals.py:raise_approval` wraps `lib.approvals.raise_(kind=type_, ...)` after the remote POST succeeds. Remote `approval_id` stored in `payload.company_approval_id`; local `source_ref` is `f'company:{remote_id}'`.
- `lib/company/reporter.py` decision-mirror hook calls `lib.approvals.decide(...)` on the local row when the remote service reports back.
- Operator-side: `lib/company/cli.py` keeps its surface; `jc approvals list` will include `kind=action|image|message` rows alongside.

### 12.8 What we don't migrate

- The IDENTITY.md frozen-section guard. `lib/self_model/frozen_sections.py:is_section_frozen` keeps its current callers AND is invoked once more inside `lib/approvals/dispatch.py:apply_self_model_diff` as a defense-in-depth check. Approving a row whose payload targets a frozen section raises at apply time even if the decide succeeded — the row sits in `approved` with `applied_at IS NULL` and `result.error='frozen_section_violation'`.
- The `lib/company/` remote service's wire protocol.

---

## 13. Security

### 13.1 Replay protection

- `callback_token` is 256 bits of entropy from `secrets.token_hex(32)`. Required on email-decide and CLI-decide paths.
- Telegram callback path validates `from_user.id == principal.telegram_user_id`; the token is not in the callback_data (would waste 64 bytes); the principal check is the gate. We accept that a compromised principal Telegram account = compromised approvals (same risk model as today).
- Decide is wrapped in `BEGIN IMMEDIATE` (§10.2); replays return `already_decided`.

### 13.2 DKIM scope

- Verification covers the message authentication (sender domain) and integrity (body hash). The decider trusts the `Authentication-Results` header **only** if the receiving MTA is one we control or is on a small allowlist (`ops/approvals.yaml:dkim.trusted_mta_hostnames`). For everyone else, verification is in-process via `dkimpy.verify(raw_message)`.
- A DKIM `d=` mismatch with `principal.email_domain` is a hard reject; the message goes to `.junk-approvals/` and the operator is alerted via Telegram (`approvals: junk DKIM rejection from <sender> — expected d=<domain>`).
- We do NOT trust SPF or DMARC alone; DKIM is the gate.

### 13.3 Callback-token entropy

`secrets.token_hex(32)` → 64 chars, 256 bits. Stored plaintext in sqlite (instance-local). The DB file is mode 0o600 (matches `state/gateway/queue.db`). No remote storage of the token by default — the `company` HTTP shim sends a hash-derived token to the remote, not the local raw token (see §13.5).

### 13.4 Prevention of self-approval

- The agent's runtime never has access to the principal's Telegram account; it routes through the bot, which speaks `sendMessage` but cannot tap inline keyboards (Telegram bots can't generate `callback_query` events for themselves).
- The agent's runtime can call `lib.approvals.decide(..., decided_by='cli')` — and this is the attack surface. Guards:
  - `decide()` requires `decision_channel != 'cli'` UNLESS `os.getuid()` corresponds to the operator's UID configured in `ops/approvals.yaml:cli.operator_uid` (resolved by `pwd.getpwnam(operator_user)`). The gateway / heartbeat / dream tick all run as the operator's UID, so this is a soft guard. Documented limitation.
  - For SENSITIVE-class approvals (`self_model_diff` targeting RULES/IDENTITY, `user_model_diff`, anything with `payload.risk_class='SENSITIVE'`), CLI decide is disabled entirely. Only Telegram or Email channels can decide. This is the hardest gate and the one that matters.
- A future "agent attestation" mechanism (sign approve calls with a key the agent doesn't hold) is out of scope.

### 13.5 Company-service interop

When `lib/company/approvals.py` runs, it generates the local row's `callback_token` independently of the remote service's token. The two never cross the wire. The remote's `decide` callback into the local store goes through `lib/company/reporter.py` which uses an authenticated WebSocket / polling channel (existing surface); on receipt the local `decide()` is called with a `decided_by='company:<remote_id>'` value and `decision_channel='system'`. This bypasses the principal check but is gated by the company-service auth, which is already trusted as part of the existing surface.

### 13.6 Audit

- `state/approvals.db` is the audit trail. Rows are immutable post-decide.
- Every dispatch is logged to `state/approvals.log` (line-per-event JSONL) for tail-able operator review.
- The Telegram card edits (`✅ Approved by you at …`) are an extra, human-friendly audit trail in the chat history. They survive `state/` rotation; they don't survive Telegram-side message deletion (which the principal can do unilaterally).

---

## 14. Open questions

1. **Default email-notify behavior.** `notify_email` defaults to per-kind in `ops/approvals.yaml`. Recommendation: default OFF for low-stakes kinds (`sender_authorize`, `email_draft`), default ON for SENSITIVE (`self_model_diff` targeting frozen sections — but those are auto-rejected; for non-frozen, default ON). Operator may want different.
2. **Per-kind callback channel.** Should `sender_authorize` ever be email-notify? Today it's Telegram-only because the operator is on the same chat. Recommendation: keep Telegram-only; flip to email if the operator is offline for >24h (a "watchdog" override). Not in v1.
3. **`message` kind backward-compat.** `lib/company/approvals.py:SUPPORTED_TYPES` includes `message`. Locally, we have `kind='message'` mapped to "the agent wants to send an outbound message via a non-default channel — operator approve?" This semantics should align with the company service's `message` semantics. Operator: confirm both mean the same thing or rename one.
4. **Audit-only rows.** Should the unified table also log auto-applied dreams (LOW/MEDIUM risk that never went through approval)? Pro: single audit surface. Con: muddies the "approval = decision" semantics. Recommendation: NO — auto-applied artifacts stay logged in `state/dreams/<utc>.md` only. The table is for things that were *decided*.
5. **Operator UID assumption for CLI gate (§13.4).** This relies on the operator running JC as their own UID. Some deployments (systemd-managed services running as a service user) break this. Recommendation: in those deployments, disable the CLI decide path entirely via `ops/approvals.yaml:cli.disabled: true` and rely on Telegram + Email.
6. **DKIM library bundling.** `dkimpy` is not currently a JC dependency. Should we vendor it, soft-import it (decide path disabled if absent), or require the operator's MTA to do verification and trust `Authentication-Results`? Recommendation: soft-import with a clear "Email decide path requires `dkimpy` — `pip install dkimpy` to enable" error.
7. **What happens to an approved row if dispatch.apply() fails repeatedly?** Current spec says it sits with `applied_at IS NULL` and is retryable. Should we add an `apply_attempts` counter and dead-letter after N failures? Recommendation: ship without; add if soak finds it.
8. **Notification of decision outcome.** When an approval is approved/rejected, do we notify the producer? Producer code is in-process for self_model/dream/user_model and already gets the return value from `decide`. For the company shim, the remote service polls. Anything else? Recommendation: no extra notification surface.

---

## 15. Test plan

### 15.1 Unit

- `lib/approvals/store.py` — schema init, INSERT, SELECT by status/kind/source_ref, idempotent `raise_(producer, source_ref)`, race-safe `decide` (BEGIN IMMEDIATE), expiry computation.
- `lib/approvals/schema.py` — payload validation per kind; rejects missing required keys; accepts/rejects each kind's contract.
- `lib/approvals/principal.py` — main chat resolver returns the right value at each level of §4 fallback; refuses non-private chat types; principal_email resolver same.
- `lib/approvals/notify/telegram.py` — card rendering (MarkdownV2 safe), inline keyboard payload, message-id roundtrip into `callback_payload`.
- `lib/approvals/notify/email.py` — email body rendering, header construction, attachment handling.
- `lib/approvals/decide/telegram.py` — `from_user.id != principal_id` rejected; valid tap flips state; double-tap idempotent; conflicting tap raises.
- `lib/approvals/decide/email.py` — DKIM-fail rejects; DKIM-pass with bad token rejects; happy path; replay protected.
- `lib/approvals/dispatch.py` — each `kind` routes to the right applier; frozen-section guard re-checked on `self_model_diff`; `apply_email_draft` calls the existing commit/discard.

### 15.2 Integration

- **Full roundtrip telegram:** producer (self_model) raises an approval; notifier sends a fake-telegram payload to a mock bot API; decider receives a fake callback_query; dispatch applies; row terminal; `lib/self_model/store.move_proposal` invoked.
- **Full roundtrip email:** same flow via email mock (in-process maildir).
- **Sender-approval migration:** existing `ops/gateway.yaml` `chat_ids` + `blocked_chat_ids` preserved during phase A; new sender triggers unified flow; on approve, `chat_ids` updated atomically (no regression on `sender-approval-config-only.md`).
- **Idempotency:** raise twice with same `(producer, source_ref)` returns same row; decide twice with same action returns terminal record both times.
- **Conflict:** decide approve then decide reject raises `ApprovalConflict`; row stays approved.
- **Expiry:** raise with `expires_in=1s`, sleep 2s, decide → returns expired status; row in `expired` state.

### 15.3 Soak

- 7-day soak on the Rachel instance: drive every producer through at least one approval. Assert: no dropped notifications, no double-applies, no orphan `applied_at IS NULL` rows, DB size growth < 1 MB/week.

### 15.4 Security

- DKIM-fail email → rejected, junk folder, operator alerted.
- Telegram callback from non-principal `from_user.id` → rejected silently, audit logged.
- CLI decide of a SENSITIVE-kind row → refused.
- Company shim raise → local row created; remote decide → local row updated.

---

## 16. Implementation order

This is a spec; it doesn't ship code. The implementation PRs (separate branches) land in this order:

**Impl PR #1 — Approvals core**
1. `lib/approvals/{store.py, schema.py, service.py, dispatch.py, principal.py}`
2. `state/approvals.db` schema migration helper
3. `ops/approvals.yaml` loader + `lib/approvals/conf.py`
4. `bin/jc-approvals` (or `jc approvals` subcommand under `bin/jc`)
5. Unit tests

**Impl PR #2 — Telegram notifier + decider**
1. `lib/approvals/notify/telegram.py`, `lib/approvals/decide/telegram.py`
2. `lib/gateway/channels/telegram.py:_handle_callback_query` routing update (`apv:` prefix)
3. Integration test (mock bot)

**Impl PR #3 — Email notifier + decider**
1. `lib/approvals/notify/email.py`, `lib/approvals/decide/email.py`
2. DKIM soft-import / verification logic
3. Hook into the existing inbox poller
4. Integration test (in-process maildir)

**Impl PR #4 — Migrate self_model + dream + user_model producers**
1. Swap stub DKIM check for `approvals.raise_` in `lib/self_model/applier.py`
2. Swap `_stage` for `approvals.raise_` in `lib/dream/apply.py`
3. Adjust `lib/user_model/cli.py:cmd_apply` to use unified API
4. Migration importers for existing pending records
5. Update CLI shims (`jc self-model approve`, `jc-dream approve`, `jc user-model apply`)

**Impl PR #5 — Migrate sender + group + email-draft producers**
1. Swap `_send_auth_prompt` → `approvals.raise_(kind='sender_authorize'|'group_authorize')`
2. Swap `_send_draft_with_buttons` → `approvals.raise_(kind='email_draft')`
3. Keep legacy `chat_auth:` / `jcemail:` callback prefixes for one release
4. Update `jc chats approve/deny` to alias

**Impl PR #6 — Company shim**
1. `lib/company/approvals.py` writes through to local table
2. `lib/company/reporter.py` mirrors remote decisions

**Impl PR #7 — Cleanup**
1. Remove hardcoded `filippo.perta@scovai.com` strings (§5.3)
2. Drop `lib/self_model/applier.py:_verify_dkim_approval` stub
3. Drop legacy `chat_auth:` / `jcemail:` callback prefixes
4. Archive `state/dreams/{pending,retained,approved,rejected}/*.json` to `state/.archive/`

---

## 17. References

- `lib/company/approvals.py`, `lib/company/client.py`, `lib/company/conf.py` — remote approval HTTP transport.
- `lib/self_model/applier.py:65-74` — stub DKIM check (target of removal).
- `lib/self_model/frozen_sections.py` — IMMUTABILE guard kept as defense-in-depth.
- `lib/dream/apply.py:13-72` — current SENSITIVE staging path.
- `lib/user_model/store.py:35-55` — JSONL proposal store (read-only after migration).
- `lib/gateway/channels/telegram.py:142-161` — current main-chat resolver.
- `lib/gateway/channels/telegram.py:413-487` — current callback_query routing.
- `lib/gateway/channels/email_dispatcher.py:230-298` — email draft approval inline keyboard.
- `lib/channels/email/authorization.py` — sender tier classification (orthogonal; not migrated).
- `lib/gateway/config.py:261-296` — `env_value` lookup (notes the os.environ leak gotcha in HOT.md).
- `docs/specs/gateway-sender-approval.md` — superseded; sender flow.
- `docs/specs/sender-approval-config-only.md` — superseded; sender state-store rewrite.
- `docs/specs/telegram-group-auth.md` — superseded; group flow.
- `docs/specs/dreaming-and-self-improve.md` §6 — superseded approval phase only; rest stays canonical.
- `docs/specs/commitments-and-reengage.md` — uses the same `BEGIN IMMEDIATE` pattern referenced in §10.2.

---

## 18. Decision log (this spec)

- **One table, not one-per-kind.** Single-table-with-discriminator beats split tables: simpler CLI, simpler audit, payload schema lives in code not in DDL.
- **Inline-keyboard taps over reply-to-approve.** One-tap UX wins; auth comes free via `from_user.id`; reply-parsing race surface avoided.
- **No webhook receiver.** Decide paths are inline-callback (telegram), DKIM-email-poll, or CLI. We don't open another HTTP port.
- **Hard fail on missing principal.** Better to refuse to enqueue an approval than to deliver one to the wrong chat. Email-fallback exists for the operator who configures it.
- **No backfill of historical decisions.** Git is the audit trail for what's already been applied.
- **Frozen-section guard kept independent.** `lib/self_model/frozen_sections.py` stays; dispatch re-checks at apply time. Two-layer defense is cheap; bypassing both would require collusion.
- **Callback-token in DB plaintext.** Instance-local DB at 0o600 is acceptable for a 256-bit secret; encryption-at-rest is a future spec if remote replicas are ever added.
- **Local audit log JSONL alongside DB.** DB for queries; JSONL for tail/grep. Same convention as `state/heartbeat/sent.log`.
