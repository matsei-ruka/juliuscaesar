# Corporate Readiness Contract

Status: Draft
Date: 2026-05-01

## Goal

Define the bar for JuliusCaesar to move from a powerful operator framework to a
corporate pilot product. This is a product and operations contract, not a narrow
security checklist.

## Product thesis

JuliusCaesar becomes valuable in corporate settings when it reliably handles
controlled operational communication:

- email and chat intake;
- memory-backed context;
- human-approved outbound actions;
- delegated background work;
- visible operational state;
- predictable recovery when something breaks.

The product should be priced and positioned around operational throughput, not
novelty. The buyer should understand what gets faster, safer to operate, and
easier to audit.

## Readiness levels

### Demo-ready

- Works for one author-controlled instance.
- Manual config edits are acceptable.
- Logs can be inspected by the author.
- Failures may require code-level debugging.

### Pilot-ready

- A second operator can install and run it from documented commands.
- `jc doctor` explains missing credentials and broken config.
- Channels can be enabled and disabled without deleting state.
- Email pending/draft state survives restarts.
- Operator decisions are durable.
- Focused tests cover the channel lifecycle.
- Rollback is a config change, not a repo surgery.

### Corporate-ready

- Setup is repeatable across instances.
- Support runbooks exist for common failures.
- Metrics reveal stuck queues, stale drafts, broken pollers, and failed sends.
- Logs are structured enough to reconstruct a customer-visible action.
- Upgrades preserve instance customization.
- The product has clear packaging: Personal Ops, Business Pilot, Corporate Ops.

## Required operator workflows

### Setup

The operator can:

- create a new instance;
- preserve existing `.env` secrets on rerun;
- test Telegram, IMAP, and SMTP independently;
- enable the gateway without reading source code;
- run `jc doctor --fix` for safe repairs.

### Email intake

The operator can:

- add trusted senders;
- block senders;
- approve unknown senders;
- inspect pending inbound messages;
- disable email polling without losing local state.

### Draft approval

The operator can:

- list pending drafts;
- inspect a full draft;
- edit draft text;
- approve exactly once;
- reject without sending;
- see how old drafts are.

### Recovery

The operator can answer:

- Is the gateway running?
- Is the email poller running?
- Are IMAP credentials valid?
- Are SMTP credentials valid?
- What is the oldest pending inbound message?
- What is the oldest draft?
- Did an outbound email send, fail, or remain pending?

## Solidity principles

- Durable state before external acknowledgment.
- Explicit lifecycle state instead of implied filesystem presence.
- Idempotent approvals and sends.
- Config writers are atomic and shared.
- Existing instance customization is never overwritten by default.
- Mainline assets and specs are not removed by feature branches.
- Compatibility aliases are allowed, but canonical commands should match the
  product domain.

## Corporate value checklist

Before calling a feature corporate-ready, answer:

- What workflow does this make faster?
- What manual follow-up does this remove?
- What decision remains human-controlled?
- What proof exists after the action?
- How does an operator recover when it fails?
- How does a second instance adopt it?
- What metric says it is healthy?

## Repricing packaging

### Personal Ops

- Telegram gateway
- Memory and transcripts
- Heartbeat tasks
- Background workers

### Business Pilot

- Email channel
- Sender approval
- Draft approval
- Setup doctor
- Operational logs

### Corporate Ops

- Multi-instance reporting
- Company dashboard integration
- Approval queues
- Runbooks
- Health metrics
- Supportable upgrades

## Phase 0 acceptance

- Branch no longer deletes mainline README assets or active specs.
- Email specs are consolidated.
- Broad KB rewrites are removed unless intentionally tied to the feature.
- Current branch diff is explainable file-by-file.

## Phase 1 acceptance

- This readiness contract exists.
- Pilot-ready and corporate-ready are distinct.
- Email channel work is tied to operator workflows.
- Future implementation issues can be prioritized by product value and
  operational solidity.

## Phase 3 acceptance

- Email state has shared helpers for pending inbound messages, outbound drafts,
  UID watermarks, and lifecycle metrics.
- `jc email doctor` reports credential presence, UID watermark, pending count,
  draft states, and oldest pending/draft ages.
- Operators can list/show/drain pending inbound messages without touching
  files directly.
- Operators can list/show/edit/approve/reject outbound drafts.

## Phase 4 acceptance

- An email operations runbook exists.
- Rollback is documented as disabling the channel without deleting state.
- Stuck pending messages, stale drafts, IMAP failures, and SMTP failures have
  explicit first-response commands.

## Phase 5 acceptance

- Packaging is documented as Personal Ops, Business Pilot, and Corporate Ops.
- The pricing story is tied to workflows and operational evidence, not model
  access or novelty.
