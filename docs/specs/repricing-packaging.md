# Repricing Packaging

Status: Draft
Date: 2026-05-01

## Goal

Turn JuliusCaesar's technical surface into product packages that explain real
operator value. This is pricing groundwork: the feature set must be legible
before the price can be defended.

## Personal Ops

For one operator running their own assistant loop.

- Telegram gateway
- Memory and transcripts
- Heartbeat tasks
- Worker spawning
- Local diagnostics

Value: saves personal coordination time and makes recurring assistant work
durable.

## Business Pilot

For a small team validating operational communication workflows.

- Everything in Personal Ops
- Email channel
- Sender approval
- External outbound draft approval
- `jc email doctor`, pending inspection, draft inspection
- Setup doctor and focused runbooks

Value: lets a second operator run and recover the system without reading source
code. This is the minimum paid pilot shape.

## Corporate Ops

For teams that need supportable operations and evidence after actions.

- Everything in Business Pilot
- Multi-instance reporting
- Queue and draft age metrics
- Approval queues
- Company dashboard integration
- Runbooks and supportable upgrades

Value: shifts the product from "automation that works for us" to "operations
software we can support."

## Packaging Rule

Do not sell a tier by model access. Sell the workflow:

- intake speed;
- approval control;
- fewer manual follow-ups;
- recovery clarity;
- evidence after customer-visible actions.
