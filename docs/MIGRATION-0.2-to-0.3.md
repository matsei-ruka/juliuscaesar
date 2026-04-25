# Migrating from 0.2.x to 0.3.0

The 0.2.x runtime was coupled to Claude Code via the Telegram MCP plugin: one
plugin = one channel = one liveness path. 0.3.0 ships the unified gateway —
multi-channel, multi-brain, optional triage. Migration is opt-in.

## Triggers

Migrate when you want any of:

- More than one channel (Telegram + Slack, Telegram + Discord, …)
- A non-Claude brain as the default (codex, gemini, opencode, aider)
- Cost-aware routing via triage (smalltalk → Haiku, analysis → Opus)
- Worker → user auto-synthesis (single message instead of two)

## Steps

```sh
# 0. Update the framework
git pull   # in your juliuscaesar checkout

# 1. Run the migrator. Default flags keep things conservative — telegram
#    only, single brain, no triage. Add flags as needed.
cd /path/to/instance
jc migrate-to-0.3                           # dry-friendly defaults
jc migrate-to-0.3 --triage openrouter       # if you want triage on day one
jc migrate-to-0.3 --enable-slack            # if you have a Slack workspace
jc migrate-to-0.3 --dry-run                 # preview changes only

# 2. Verify
jc doctor

# 3. Start the gateway daemon (or restart the watchdog)
jc gateway start
# OR
jc watchdog tick

# 4. Send a test message in Telegram and confirm the reply.

# 5. (Optional) Enable triage later
$EDITOR ops/gateway.yaml                    # set triage: openrouter
echo OPENROUTER_API_KEY=sk-or-... >> .env
jc gateway reload
```

## What the migrator does

- Reads `ops/watchdog.conf` and `.env` for current channel + chat id values.
- Writes `ops/gateway.yaml` with conservative defaults.
- Backs up the previous `gateway.yaml` and `watchdog.conf` to timestamped
  `.bak` files so rolling back is `mv` and a watchdog restart.
- Sets `RUNTIME_MODE=gateway` in `watchdog.conf`.

## Rollback

```sh
# Edit watchdog.conf and set RUNTIME_MODE=legacy-claude
# (or move the .bak files back into place)
$EDITOR ops/watchdog.conf
jc gateway stop
jc watchdog tick
```

The legacy live-Claude path will keep working through 0.4.x with a
deprecation warning. It is removed in 0.5.0.

## Multi-brain: enabling Codex / Gemini / etc.

```yaml
# ops/gateway.yaml
default_brain: codex
default_model: gpt-5
brains:
  codex:
    sandbox: workspace-write
    timeout_seconds: 600
```

Per-channel and per-message overrides are also supported:

- Per channel: `channels.telegram.brain: codex`
- Per message inline: `[opus] explain quantum tunneling`
- Per conversation: `/brain opus` (sticks until idle)

## Worker auto-synthesis

`jc workers spawn ...` completion now writes
`state/events/worker-<id>-<status>.json`. The `jc-events` channel picks it up
and the configured brain synthesizes a single user-facing reply. To force the
0.2.x behaviour (raw send_telegram), export
`WORKERS_NOTIFY_MODE=telegram-direct`.

## Things that did not change

- L1/L2 memory layout
- Heartbeat task schema (`heartbeat/tasks.yaml`)
- Voice tooling (`lib/voice/`)
- Adapter shell-script contract (`lib/heartbeat/adapters/<tool>.sh`)
