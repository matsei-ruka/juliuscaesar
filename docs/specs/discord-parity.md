# Discord channel — Telegram parity

**Status:** proposed
**Date:** 2026-06-14
**Scope:** specification only; no implementation in this PR
**Touches:** `lib/gateway/channels/discord.py`, `lib/gateway/config.py`
(ChannelConfig), `lib/supervisor/delivery.py` (Discord card delivery),
`lib/gateway/channels/registry.py`, `channel_lifecycle.py`,
`docs/kb/subsystem/channels.md`
**Related:** `docs/specs/gateway-sender-approval.md` (Telegram auth flow),
`docs/specs/supervisor-action-background-race.md` (card actions)

## 0. Summary

The Discord channel is ~15% of the Telegram channel. `discord.py` is 133 lines:
inbound on `@mention`/DM, plain-text outbound truncated at 2000 chars. No auth,
no allowlist, no media, no reactions, no card buttons, no markdown bridge.

This spec brings Discord to Telegram parity in three tracks, matching the three
asks:

1. **Feature parity** — outbound chunking + markdown bridge, media in/out,
   reactions, typing, and **supervisor cards with working Stop/Background
   buttons** (Discord components + interaction callbacks).
2. **Auth** — guild/channel allowlist + blocklist + operator approval flow, and
   **mention-to-answer** in guild channels (tag the bot to make it reply),
   mirroring `telegram_routing.should_process_message`.
3. **Dual-channel binding** — one instance reachable on **both** Telegram (main
   chat) and a Discord channel at once. Confirmed already possible at the
   transport layer; this spec nails down the config and the
   shared-vs-separate-context decision.

## 1. What exists today (baseline)

| Area | Telegram | Discord today |
|------|----------|---------------|
| Inbound transport | long-poll `getUpdates` | websocket (`discord.py`), async thread |
| Answer gating | allowlist + blocklist + approval + mention parsing | `@mention` or DM only — **no allowlist, any guild** |
| Outbound text | chunked, MarkdownV2 + plain-text fallback | single `channel.send`, hard 2000-char truncate |
| Media in | photo / audio+ASR / doc / video | none |
| Media out | photo / voice | none |
| Reactions / typing | busy emoji + `sendChatAction` | none |
| Supervisor cards | send/edit/delete **+ Stop/Background buttons** | send/edit/delete **plain text, no buttons** |
| Slash commands | `/help` `/models` `/compact` | none |

`conversation_id` is already source-namespaced (Telegram `chat_id[:thread]`,
Discord `channel_id:thread_id`), and the supervisor's `_MultiChannelSender`
already routes cards by `event.source`. So the **plumbing for multi-channel and
for Discord cards exists** — Discord is feature-incomplete, not unwired.

## 2. Track A — feature parity

### A1. Outbound: chunking + markdown bridge
Replace the 2000-char truncate (`discord.py` `send()`) with the
paragraph/fence-aware chunker already used for Telegram
(`telegram_outbound.py:194`), retargeted to Discord's 2000 limit. Add a
`to_discord_markdown()` formatter: Discord supports `**bold**`, `*italic*`,
`__underline__`, `` `code` ``, ```` ```fenced``` ````, `> quote`, `||spoiler||`
— but **not** MarkdownV2 escaping rules. Bridge from the internal markdown
representation, never ship raw MarkdownV2 backslash-escapes. Fallback to plain
text on any formatting error, same discipline as Telegram.

### A2. Media
- **In:** on `message.attachments`, download to
  `state/voice/inbound/{photos,docs}/…`, run ASR on audio, fuse video like
  Telegram (`telegram_media.py`). Reuse the same sinks so the brain sees
  identical inputs regardless of channel.
- **Out:** photo and voice send via `channel.send(file=...)`.

### A3. Reactions + typing
Busy-state reaction on the inbound message (`message.add_reaction`) and
`channel.typing()` during dispatch — parity with the Telegram busy emoji and
`sendChatAction`.

### A4. Supervisor cards with buttons (the headline)
Discord cards today are plain text (`delivery.py:363`). Upgrade:
- **Embed** for the card body (phase, activity, elapsed) — richer than text,
  editable in place via `PATCH .../messages/{id}` (edit already works).
- **Components** — an action row with `Stop` / `Background` buttons
  (`custom_id: act:stop:<token>` / `act:bg:<token>`), the same token contract
  the Telegram keyboard uses (`cards.py:83`).
- **Interaction handler** — `on_interaction` maps a button click to the
  `actions_registry` lookup (`state/actions/event-<id>.json`) and ACKs the
  interaction. This is the Discord twin of the Telegram callback-query handler.

Gating stays behind `actions.enabled`, identical to Telegram.

### A5. Slash commands (low priority)
`/help`, `/models`, `/compact` as Discord application commands. Nice-to-have;
sequence last.

## 3. Track B — auth

### B1. Config schema
Extend `ChannelConfig` (already extensible — Discord uses 2 of ~16 fields).
Reuse the existing `chat_ids` / `blocked_chat_ids` tuples as the Discord
allow/block sets, keyed by **channel id** (and optionally `guild_id` for a
whole-server allow). No new field names where the Telegram ones fit — keep the
mental model identical:

```yaml
discord:
  enabled: true
  bot_token_env: DISCORD_BOT_TOKEN
  chat_ids: ['1234567890']          # allowed channel (or guild) ids
  blocked_chat_ids: []
```

### B2. Default-deny + approval flow
Mirror `gateway-sender-approval.md`: when the bot is added to a new guild
(`on_guild_join`) or sees a first message from a non-allowlisted channel, send
the **operator** (Telegram main DM — the operator already lives there) an
approval prompt with guild/channel name + member count + Allow/Deny. On Allow,
write the channel id into `chat_ids`; on Deny, into `blocked_chat_ids` (and
optionally leave the guild). Default-deny until approved — today Discord answers
any mention in any guild, which is the security hole.

Open choice: the approval prompt can land on Telegram (operator's existing main
DM) or on a designated Discord operator DM. Leaning Telegram for a single
approval surface — see §5.

### B3. Mention-to-answer in guild channels ("tagging in the group to answer")
Port `telegram_routing.should_process_message` to Discord:
- DM → always answer.
- Guild channel → answer only if the bot is **@mentioned**, the message
  **replies to the bot's** message, or the channel is explicitly allowlisted as
  "always answer". Otherwise stay silent.

This is the direct analog of the Telegram group rule and what "tagging in the
group to answer" asks for. `discord.py` already has `client.user in
message.mentions`; the work is adding the reply-to-bot case, the allowlist
check, and the silent-default.

## 4. Track C — dual-channel on one instance

**Finding: already works at the transport layer.** `ChannelLifecycle.start()`
builds every enabled channel into its own daemon thread sharing one `enqueue()`
(`channel_lifecycle.py:91`); Telegram poll loop and Discord async client coexist
today, and `conversation_id` won't collide across sources. So the literal ask —
"one bot, main chat on Telegram **and** connected to a Discord channel" — is a
config change once Discord auth (Track B) exists:

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

Both feed the same brain; each conversation keeps its own session, history, and
supervisor cards on its origin channel.

**The one real design question: shared context or separate?** Two readings of
"connected to a Discord channel":
- **(default) Separate conversations** — Telegram main chat and the Discord
  channel are independent threads to the same agent. Clean, no cross-talk,
  works the moment Track B lands. Recommended default.
- **(bridged) Shared context** — a message in either surface is visible to the
  other, one merged conversation spanning two channels. This needs a
  conversation-aliasing layer (map two `(source, conversation_id)` pairs to one
  session) and a rule for which channel a reply/card goes to. Materially more
  work and more failure modes.

This spec builds the default; bridging is flagged as an open question (§5.1),
not built unless you say so.

## 5. Open questions

1. **Dual-channel: separate or bridged context?** Default is separate (§4). Do
   you actually want one merged conversation across Telegram + Discord, or just
   the same agent answering on both independently? This changes the size of the
   work the most — answer first.
2. **Approval surface.** New-guild approval prompts → Telegram operator DM
   (single approval inbox) or a Discord operator DM? Leaning Telegram.
3. **Allowlist granularity.** Allow per-channel, per-guild, or both? Telegram
   has only the flat `chat_ids`; Discord has a guild→channel hierarchy that
   could allow "whole server" vs "this channel."
4. **Library dependency.** Keep `discord.py` (async, mature, pulls an event loop
   into a thread) or move to raw REST + gateway websocket to match the framework's
   sync style? `discord.py` is faster to parity; raw is fewer deps. Leaning keep
   `discord.py`.
5. **Buttons without a registered app.** Discord message components require the
   bot's application to be configured for interactions. Confirm the existing bot
   apps have the interactions endpoint / intents enabled, or document the
   one-time setup per agent.

## 6. Implementation sequencing (when greenlit)

Specs-first — no code in this PR. Suggested order:
1. Track B (auth: schema + default-deny + mention-to-answer) — **security first**,
   it's the open hole.
2. Track A1–A3 (chunking, markdown bridge, media, reactions).
3. Track A4 (cards + buttons + interaction handler) — the headline feature.
4. Track C config + docs (works once B lands; bridging only if §5.1 says so).
5. Track A5 (slash commands).
