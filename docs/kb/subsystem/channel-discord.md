---
title: Discord gateway channel
section: subsystem
status: active
code_anchors:
  - path: lib/gateway/channels/discord.py
    symbol: "class DiscordChannel:"
last_verified: 2026-04-25
verified_by: claude
related:
  - subsystem/gateway-queue.md
---

## Summary

Inbound DMs and `@`-mentions from Discord arrive via the `discord.py` library's
gateway intent. Outbound replies go to the originating channel (or thread
when present).

## Setup

1. Create a Discord bot via the developer portal with the **MESSAGE CONTENT**
   intent enabled.
2. Add the bot to your server.
3. Set `DISCORD_BOT_TOKEN` in `<instance>/.env`.
4. Enable the channel in `ops/gateway.yaml`:
   ```yaml
   channels:
     discord:
       enabled: true
       bot_token_env: DISCORD_BOT_TOKEN
   ```
5. `pip install discord.py` (gated optional dependency).

## Behavior

- Bots and self-messages are ignored.
- Only DMs and explicit mentions enqueue events. Channel-wide chatter is not
  consumed.
- `conversation_id = "<channel_id>:<thread_id>"` so threaded DMs resume the
  same brain session.

## Invariants

- The bot token never leaves `<instance>/.env`.
- If `discord.py` is missing the channel logs and exits cleanly — gateway
  keeps running.
