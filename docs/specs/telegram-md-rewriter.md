# Telegram MarkdownV2 — render formatting properly, no more raw asterisks

**Status:** Spec
**Author:** Rachel
**Date:** 2026-04-26

## Problem

`TelegramChannel.send()` posts to `sendMessage` without `parse_mode`. Telegram
then renders the body as plain text, so any markdown the brain emits leaks
through literally:

- `*bold*` → `*bold*` on screen
- `[link](https://x)` → `[link](https://x)` on screen
- ```` ```code``` ```` → seven literal backticks plus the code

Comparison: nanobot (BNESIM internal alerts bot) sends bold messages that
render correctly because it sets `parse_mode`. Telegram's Bot API supports
`Markdown` (V1, deprecated), `MarkdownV2`, and `HTML`. We pick **MarkdownV2**:
full feature parity, future-proof, and once an escaper is in place the brain
can write naturally with markdown and have it render.

## Goals

- Outbound Telegram text uses `parse_mode=MarkdownV2` and renders bold /
  italic / code / links as intended.
- Brain output passes through a deterministic escaper that:
  - normalizes CommonMark spans (`**bold**`, `*italic*`/`_italic_`, `` `code` ``,
    `[t](u)`, fenced code) to V2 syntax
  - escapes V2 reserved chars (`_*[]()~`>#+-=|{}.!`) outside intentional
    spans so Telegram does not 400 on a bare `.` at end of sentence
- Failure is silent and safe: if Telegram returns 400 (`Bad Request: can't
  parse entities`), retry once without `parse_mode` so the message still
  reaches the user.
- No LLM in the hot path. The escaper is regex + string ops, free, fast.

## Non-goals

- No `parse_mode=HTML`. MarkdownV2 has full feature set; HTML adds nothing.
- No `parse_mode=Markdown` (V1). Deprecated and lossy.
- No Slack/Discord support in this PR — out of scope.
- No streaming. `sendMessage` is one-shot.
- No reformatting beyond escaping. Brain emits the words; escaper makes them
  Telegram-safe.

## MarkdownV2 reserved characters

Per Telegram Bot API docs, these chars must be backslash-escaped if not part
of an entity:

```
_ * [ ] ( ) ~ ` > # + - = | { } . !
```

Inside `pre`/`code` entities, only `` ` `` and `\` need escaping. Inside the
URL part of `inline link` and `custom emoji`, only `)` and `\` need escaping.
We honor those carve-outs.

## Conversion + escape algorithm

Implemented in `lib/gateway/format/escaper.py`. `to_markdown_v2(text) -> str`.

### Pass 1 — extract intentional spans

Find and replace with placeholder tokens (NUL byte + index, never collides
with user text):

| Pattern | Captured form | V2 output |
|---|---|---|
| ``` ```lang\n…\n``` ``` (multiline fenced) | code body + lang | ```` ```lang\n…\n``` ```` (escape `` ` `` `\` inside) |
| ``` `…` ``` (inline) | code text | `` `…` `` (escape `` ` `` `\` inside) |
| `[text](url)` | text + url | `[escaped_text](escaped_url)` |
| `**…**` | bold text (recurse) | `*…*` (V2 bold) |
| `*…*` (single, not adjacent to word boundary like `2*3`) | italic text | `_…_` (V2 italic) |
| `_…_` (single, word-boundary) | italic text | `_…_` (V2 italic) |
| `~~…~~` | strikethrough | `~…~` |
| `__…__` (rare from Claude) | bold | `*…*` |

Order matters: code fences first (their content must NEVER be reformatted),
then inline code, then links (URL must NEVER be reformatted), then
emphasis.

### Pass 2 — escape reserved chars

In the remaining text (with placeholders for spans), escape every reserved
char with backslash. Then restore the placeholders with their pre-formatted
V2-syntax replacements.

### Lists / headings

CommonMark headings (`# `, `## `) and bullet lists (`- `, `* `, `+ `) have
no V2 equivalent. We strip the leading marker and uppercase the heading line
(or prepend an emoji marker the brain provided). Bullets become `•`.

This matches Rachel's existing aesthetic ("ALL-CAPS + emoji + bullets") and
avoids over-escaping list dashes.

### What we do NOT touch

- Plain text with no markdown → passes through with reserved chars escaped
  (so a sentence like `Now: 13:45.` becomes `Now: 13:45\.` and renders
  identically to the user).
- URLs in plain text (not wrapped in `[](…)`) → escaped per the V2 rules
  (the `.` and `-` get backslashes), Telegram still auto-links.
- Emoji → untouched.

## Hook point

`TelegramChannel.send()` (lib/gateway/channels/telegram.py:293) — BEFORE the
`sendMessage` HTTP call, after the voice-fallback short-circuit:

```python
text = response[:4096]
escaped = to_markdown_v2(text)
payload = {
    "chat_id": chat_id,
    "text": escaped,
    "disable_web_page_preview": True,
    "parse_mode": "MarkdownV2",
}
```

If Telegram returns `400 Bad Request: can't parse entities…`, we re-POST
once with no `parse_mode` and the original (unescaped) text, then log a
warning. Message still reaches the user; the team gets a signal that the
escaper missed a case worth fixing.

`send_voice` is NOT touched — voice replies bypass markdown entirely.

## Config

No new config knobs. `parse_mode=MarkdownV2` is always-on for Telegram text
sends. (The decision is intentional: half the value of the feature is
removing the "should I escape?" cognitive overhead from prompts.)

The previous design's `FormatConfig` (LLM backend, model, retries) is
removed — no LLM in the path.

## Failure modes

| Failure | Behavior |
|---|---|
| Escaper produces malformed V2 (regex bug, unmatched span) | Telegram 400 → retry without `parse_mode` → text still delivered. Warning logged. |
| Brain emits a literal `\` followed by something that looks like an entity | Escaper keeps the `\` literal (escapes it as `\\`). Send succeeds. |
| Brain emits HTML tags by mistake (`<b>x</b>`) | Treated as text; `<` `>` are NOT V2-reserved, so they pass through unescaped and render literally. (Decision: tolerate, don't try to convert HTML→V2.) |
| Brain emits a code fence with backticks inside the content | Escaper escapes inner backticks. Fence still wraps. |
| Empty string / whitespace-only response | Existing guard short-circuits. Nothing sent. |

## Logging

Single structured line per send:

```
telegram.send.formatted parse_mode=MarkdownV2 orig_len=<n> esc_len=<n> spans=<n>
```

On the 400 retry path:

```
telegram.send.parse_error retrying without parse_mode err=<short>
```

## Test plan

`tests/gateway/test_format.py` (new):

- `EscaperTests`
  - plain text gets reserved chars escaped (`Hello, world.` → `Hello, world\.`)
  - bold `**x**` → `*x*` with rest escaped
  - italic `*x*` → `_x_`
  - inline code `` `x` `` preserved (no inner escape unless ` or \)
  - fenced code ```` ```py\nx=1\n``` ```` preserved with lang tag
  - markdown link `[t](u)` → `[t](u)` with text and URL escaped per rules
  - URL with underscores in path NOT broken (`/wiki/Foo_bar` stays)
  - heading `## title` → `TITLE` (caps, marker stripped)
  - bullet `- item` → `• item`
  - mixed: bold + plain + period works

`tests/gateway/test_channels.py` (extend):

- `TelegramSendParseModeTests`
  - `send` always sets `parse_mode=MarkdownV2` in payload
  - `send` text is escaped (sentinel `.` becomes `\.` in payload)
  - on Telegram 400, second POST omits `parse_mode` and uses original text
  - on 400 fallback, warning is logged

Pass criteria: `python3 -m unittest tests.test_gateway tests.gateway.test_channels tests.gateway.test_format` all green.

## Decisions to defer

- **Escaper for Slack/Discord.** Different dialects; not in this PR.
- **Smart code-block language detection.** Whatever the brain writes after
  the opening triple-backtick is preserved as the lang tag. We don't sanity
  check.
- **Re-running the escaper on its own output.** Idempotent if input is
  already valid V2; no current need.
- **Custom emoji `[👍](tg://emoji?id=…)`.** Not generated by Claude; defer.
- **Quoted-text spoilers / underline / spoiler entities.** Brain doesn't
  emit them. Add later if needed.

## Prompt updates

Outside the gateway code, this PR also flips the framing in:

- `memory/L1/RULES.md` — Telegram block: from "no markdown symbols" to
  "use MarkdownV2 freely; gateway escapes."
- `memory/L2/learnings/telegram-formatting.md` — supersede the old
  no-markdown rule with the new render-via-V2 stance.

These edits live in the Rachel instance repo, NOT juliuscaesar — separate
commit on the rachel_zane side.
