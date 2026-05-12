---
slug: templates/re-engagement/README
title: Re-engagement templates
layer: L2
type: reference
state: draft
created: TODO
updated: TODO
last_verified: ""
tags: [template, re-engagement]
links: []
---

# Re-engagement templates

This directory holds persona-authored message templates for `reengage_tick`.

Each tracked chat in `ops/reengage.yaml` points `touch_1` through `touch_4` at
markdown files under `memory/L2/`. V1 requires templates; it does not ask a
brain to invent touch text at dispatch time.

Tone ladder:

1. Light check-in.
2. Useful nudge with context.
3. Direct follow-through.
4. Clean close, no pressure.

Hard stops:

- Maximum four touches per silence episode.
- Any fresh inbound reply cancels pending re-engagement commitments.
- The framework queues and cancels messages; the persona owns the words.
