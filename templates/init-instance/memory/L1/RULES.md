---
slug: RULES
title: Standing Rules & Feedback
layer: L1
type: rules
state: draft
created: TODO
updated: TODO
last_verified: ""
tags: [rules, feedback]
links: [USER, IDENTITY]
---

# Standing rules

Corrections, validated non-obvious choices, hard-won lessons. Lead with the rule, then **Why:** and **How to apply:**.

## Instance awareness

Why: Claude starts fresh, but this instance carries durable context.
How to apply: Read L1 memory at session start. Use `jc memory search` and
`jc memory read` for L2 context.

## Runtime checks

Why: The assistant depends on local binaries, credentials, and a live Claude
session.
How to apply: Use `jc doctor` when behavior feels broken or after setup.

## Work routing

Why: The live session should stay responsive.
How to apply: Do quick answers inline. For longer implementation, research,
scaffolding, or test-heavy work, use `jc workers spawn` when available.
