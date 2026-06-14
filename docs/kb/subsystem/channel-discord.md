---
title: Discord gateway channel
section: subsystem
status: active
code_anchors:
  - path: lib/gateway/channels/discord.py
    symbol: "class DiscordChannel:"
  - path: lib/gateway/channels/discord_routing.py
    symbol: "def should_process_message"
  - path: lib/supervisor/delivery.py
    symbol: "def send_card_discord"
last_verified: 2026-06-14
verified_by: r.zane
related:
  - subsystem/gateway-queue.md
  - subsystem/channels.md
---

## Summary

Inbound DMs and `@`-mentions from Discord arrive via the `discord.py` library's
gateway intent. Outbound replies go to the originating channel. Brought to
Telegram parity in three tracks (see `docs/specs/discord-parity.md`): an
**auth allowlist + approval flow**, **mention-gating** in guild channels, and
**supervisor cards rendered as embeds with working Stop/Background buttons**.

## Setup

1. Create a Discord bot via the developer portal with the **MESSAGE CONTENT**
   intent enabled. For the card buttons to work, the application's
   **interactions** must be enabled (default for any bot added with the
   `bot` + `applications.commands` scopes).
2. Add the bot to your server.
3. Set `DISCORD_BOT_TOKEN` in `<instance>/.env`.
4. Enable the channel in `ops/gateway.yaml`:
   ```yaml
   channels:
     discord:
       enabled: true
       bot_token_env: DISCORD_BOT_TOKEN
       chat_ids: []          # allowed channel ids (or guild ids); default-deny
       blocked_chat_ids: []
   ```
5. `pip install discord.py` (gated optional dependency).

## Authorization (default-deny)

`chat_ids` is the allowlist, mirroring Telegram. Ids may be:

- a **channel id** — allowlists that one channel, and marks it *always-answer*
  (no `@mention` required, the analog of a 1:1 Telegram chat); or
- a **guild id** — authorizes every channel in that server, but those channels
  still require an `@mention` to answer.

`blocked_chat_ids` overrides the allowlist. DMs are implicitly authorized.

**Approval flow.** A first message from an unknown channel sends an approval
prompt to the operator's **Telegram** main DM (single approval surface). The
operator's *Allow* / *Deny* tap is written back into
`channels.discord.chat_ids` / `blocked_chat_ids` by the Telegram channel
(`dcauth:` callback prefix). Allowed → processed on the next message; the
config cache is busted immediately, no restart needed.

## Mention-gating

`discord_routing.should_process_message`:

- DM → always answer.
- Guild channel → answer only if the bot is `@mentioned`, the message replies
  to one of the bot's messages, or the channel id is explicitly allowlisted as
  always-answer. Otherwise stay silent.

## Supervisor cards + interactions

When `gateway.actions.enabled: true`, a supervisor card on Discord renders as an
**embed** with an action row of **✋ Stop** (danger) and **🔄 Background**
(secondary) buttons. Button `custom_id` uses the same token contract as the
Telegram inline keyboard: `act:<verb>:<short_token>`.

A button click arrives as a Discord interaction; `DiscordChannel._on_interaction`
is the twin of Telegram's callback-query handler — it parses the `custom_id`,
authorizes the clicking channel, resolves the token via `actions_registry`,
calls `actions.stop_session` / `actions.background_session` (off the event
loop), acks the interaction, and edits the card to its terminal state with the
buttons removed.

## Dual-channel (Telegram + Discord at once)

Both channels can be enabled on one instance. Each runs in its own daemon
thread sharing one `enqueue()`; `conversation_id` is source-namespaced so they
never collide. The two surfaces are **independent conversations** (not
bridged) — a Telegram DM and a Discord channel are separate sessions, separate
histories, separate cards on their origin channel. Example:

```yaml
channels:
  telegram:
    enabled: true
    chat_ids: ['<operator_dm>', '<group>']
  discord:
    enabled: true
    bot_token_env: DISCORD_BOT_TOKEN
    chat_ids: ['<discord_channel_id>']
```

## Behavior

- Bots and self-messages are ignored.
- Outbound text is paragraph-chunked to Discord's 2000-char limit (no silent
  truncation).
- `conversation_id = "<channel_id>:<thread_id>"`.

## Invariants

- The bot token never leaves `<instance>/.env`.
- If `discord.py` is missing the channel logs and exits cleanly — gateway
  keeps running.
- Authorization is config-only (`ops/gateway.yaml`); no DB read on the auth
  path.
