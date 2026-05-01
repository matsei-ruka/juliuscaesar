# Repricing Corporate Readiness Report

Date: 2026-05-01
Branch: `feat/email-channel`

## Executive readout

JuliusCaesar is not corporate-ready yet, but it is close enough for a serious
internal pilot. The email branch adds real enterprise-shaped value: IMAP/SMTP as
a first-class channel, sender approval, pending-message drain, Telegram operator
notifications, and simpler setup behavior.

The gap is not another round of abstract security hardening. The gap is product
solidity: predictable setup, clear operator workflows, no overlapping specs,
no branch collateral damage, measurable reliability, supportable failure modes,
and a business-facing value story.

Call the larger activity **Repricing**: make the product worth more before
talking about charging more.

## Review findings captured

### Sender approval / Codex brain specs

1. `docs/specs/sender-approval-config-only.md:145-156` — blocklist behavior is
   contradictory. One section requires an early blocklist short-circuit before
   record/audit work, while the file-by-file section says to drop the early
   poll blocklist check.
2. `docs/specs/sender-approval-config-only.md:296-297` — migration references
   `jc chats migrate-to-config --prune-db`, but the CLI has no such flag and
   `auth_status` is `NOT NULL`.
3. `docs/specs/codex-main-brain-hardening.md:364-366` — the proposed
   `--ask-for-approval never` Codex flag is not exposed by the installed
   `codex exec --help`.
4. `docs/specs/codex-main-brain-hardening.md:359-362` — read-only Codex chat
   default is described as necessary but is not an acceptance requirement.

### Email specs

1. `docs/specs/email-channel.md:190-195` — the original spec treated the IMAP
   `From:` header as verified identity. For corporate auto-processing, sender
   identity needs a deliverability/authentication signal or a deliberately
   constrained pilot policy.
2. `docs/specs/email-channel.md:62-69` — "ignore and retry next poll" conflicts
   with a UID watermark. Unknown messages must be persisted before the watermark
   advances and later drained from local state.
3. `docs/specs/email-draft-approval.md:54-65` — draft storage used raw sender
   addresses as path segments. Storage needs stable safe IDs.
4. `docs/specs/email-draft-approval.md:25-40` — the external sender flow mixed
   inbound sender approval with outbound draft approval. These are separate
   workflows.

## Repricing activity

### Phase 0: Merge discipline

- Preserve mainline assets and specs that the email branch accidentally removed.
- Restore broad KB/README churn from `main` unless a change is explicitly tied
  to the email feature.
- Merge `email-channel` and `email-draft-approval` into one email operating
  spec.
- Keep the minimal setup spec, because it supports corporate onboarding.

### Phase 1: Product contract

- Define what "corporate-ready" means in this repository.
- Separate pilot-ready from GA-ready.
- Make operator workflows explicit: setup, approval, draft review, rollback,
  failure triage, and evidence.
- Treat reliability and supportability as product features.

### Phase 2: Email feature review

- Review the branch implementation against the consolidated email lifecycle:
  fetch, persist, classify, enqueue, draft, approve, send, observe.
- Decide whether `jc-chats --email` remains a compatibility alias or moves to
  `jc email`.
- Close the gap between implemented `allowed/blocklist` behavior and planned
  `trusted/external/blocklist` behavior.

### Phase 3: Solidity refactor

- Introduce one email state store for UID watermarks, pending inbound messages,
  outbound drafts, send results, and failure state.
- Make UID advancement conditional on local durable state.
- Use stable safe IDs for pending and draft records.
- Move config mutation into shared writer helpers.
- Add `jc email doctor`, `test-imap`, `test-smtp`, `pending`, and `drafts`
  commands.

### Phase 4: Corporate operations

- Add lifecycle metrics: fetched, pending, approved, drafted, sent, failed,
  approval age, draft age.
- Add runbooks for stuck drafts, SMTP failures, IMAP failures, config mistakes,
  and gateway downtime.
- Add rollback: disable email channel without deleting state.
- Add sample corporate instance configuration.

### Phase 5: Value packaging

- Personal Ops: Telegram, memory, heartbeat, workers.
- Business Pilot: email channel, approvals, setup doctor, audit trail.
- Corporate Ops: dashboard/company integration, multi-instance reporting,
  explicit operator workflows, runbooks, supportability.

## Readiness verdict

Pilot-ready after Phase 0/1 plus focused email acceptance tests.

Corporate-ready only after the solidity refactor and operations layer exist.
The product will feel corporate-ready when boring things become boring:
setup, credential testing, message persistence, approvals, failed sends, and
rollbacks.
