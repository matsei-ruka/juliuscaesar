# Architecture

## Two-repo model

**Framework** (this repo — `juliuscaesar`):
Reusable code. Zero user data. Published as open source.

**Instance** (separate private repo per user, for example `julius_instance`):
User identity, memory contents, skill configs, credentials. Owned and version-controlled by the user.

## Process model

```
cron / watchdog / channel events
        │
        ▼
  ~/.local/bin/jc <subcommand>          ← framework binaries (installed globally)
        │
        ▼
  reads $JC_INSTANCE_DIR (default: cwd) ← instance directory
        │
  ┌─────┼──────┬──────┬──────┬──────┐
  ▼     ▼      ▼      ▼      ▼      ▼
memory/ heartbeat/ voice/ ops/  skills/ .env
  (instance data — versioned in the user's private repo)
```

## The `claude` binary

JC never simulates Claude Code. When a task needs AI:

1. Task config names a **tool** (claude, codex, gemini, opencode, aider) and optional model.
2. JC invokes the tool's **native CLI** as a subprocess (e.g. `claude -p`).
3. User supplies authentication via the tool's own login flow (e.g. `claude /login`).
4. Subscription-bound usage stays on the user's subscription — no API key injection, no session spoofing.

This is what makes JC policy-safe compared to API-simulating tools.

## Layered memory (L1 / L2)

Inspired by Karpathy's LLM Wiki pattern + MehmetGoekce's cache architecture:

- **L1** (always loaded, ~10-20 files): identity, standing rules, hot cache. Loaded at every session/task start.
- **L2** (on-demand, tens to thousands of files): people, projects, learnings, references. Surfaced via FTS5 search when relevant.
- Entries are markdown with YAML frontmatter. `[[wikilinks]]` build a backlinks graph.
- `jc memory search "query"` returns ranked snippets; `jc memory read <slug>` loads a full entry.
- Obsidian-compatible vault layout — open the instance's `memory/` dir as a vault for graph view.

## Heartbeat (scheduled tasks)

Three-layer runner:

1. **Deterministic (bash pre_fetch)**: data collection — IMAP, APIs, scrapers. Zero tokens.
2. **Triage (hash delta)**: exit silently if nothing changed since last run.
3. **Synthesis (adapter call)**: only if delta present, invoke the configured tool to produce output.

Output is sent via a raw-API channel client (Telegram, etc.) that does **not** depend on the live Claude session being alive. `sent.log` records every sent message so the live session can resolve user replies to past cron messages by context lookup.

## Telegram outbound formatting

Outbound text on Telegram is sent with `parse_mode=MarkdownV2` so brain responses render formatting natively (bold, italic, monospace, fenced code, clickable links, strikethrough). Brains write plain markdown; the gateway's escaper (`lib/gateway/format/escaper.py:to_markdown_v2`) handles every V2 reserved character (`_*[]()~``>#+-=|{}.!`) deterministically.

- Two-pass converter: extracts intentional spans (code, links, bold, italic, strike) into placeholders, escapes all reserved chars in the remaining text, then restores spans with V2 syntax.
- Headings (`# `, `## `) → ALL-CAPS lines; bullets (`- `, `* `) → `•` (no native V2 equivalent).
- Safety net: if Telegram returns 400 (parse_error), the gateway retries once without `parse_mode` using the original text — messages always ship.
- Spec: [`docs/specs/telegram-md-rewriter.md`](./specs/telegram-md-rewriter.md).

## Watchdog

Cron-triggered supervisor for the live Claude session. Detects crashes (including Claude CLI auto-updates that kill the running process), restarts with `--resume <session-id>` from a conf file, pings the user via Telegram on state transitions.

## Workers (on-demand background agents)

Heartbeat is for *scheduled* tasks (cron-driven). Workers are for *on-demand* tasks — dev work the user asks for interactively, that shouldn't block the chat session.

```
  main session (chat, Sonnet)
      │
      │  spawns via `jc workers spawn`
      ▼
  state/workers.db  ◄─── status updates ◄─── worker process (detached)
      │
      ▼                                       │
  jc workers list/show/tail                   ▼
                                        send_telegram.sh
                                              │
                                              ▼
                                        user's Telegram DM
```

State is in `<instance>/state/workers.db` (SQLite) + per-worker `state/workers/<id>/{prompt,log,result}`. Each worker runs detached via a double-fork + setsid, so it survives the spawning shell and the main Claude session crashing. Completion is pushed to Telegram as a plain-text summary.

**When the main session should spawn vs. do inline:**
- Quick answers, explanations, chat → inline
- Single edit to one file → inline
- Multi-file refactors, scaffolding, research reports → spawn
- Anything iterative or with tests to run → spawn

See `docs/specs/workers.md` for the full spec.
