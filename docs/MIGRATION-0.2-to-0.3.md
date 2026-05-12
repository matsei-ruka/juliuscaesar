# Migrating Older Instances To The Unified Gateway

The 0.2.x runtime was coupled to Claude Code via the Telegram MCP plugin: one
plugin = one channel = one liveness path. The CalVer gateway releases ship the
unified gateway: multi-channel, multi-brain, optional triage.

## Triggers

Migrate when you want any of:

- More than one channel (Telegram + Slack, Telegram + Discord, …)
- A non-Claude brain as the default (codex, gemini, opencode, aider)
- Cost-aware routing via triage (smalltalk → Haiku, analysis → Opus)
- Worker → user auto-synthesis (single message instead of two)

## Steps

```sh
# 0. Update the framework and run release hooks. Default hook behavior keeps
#    things conservative: telegram-only, single brain, no triage.
jc update --instance-dir /path/to/instance --yes

# 1. Move into the instance and verify.
cd /path/to/instance
jc doctor

# 2. Start the gateway daemon (or restart the watchdog)
jc gateway start
# OR
jc watchdog tick

# 3. Send a test message in Telegram and confirm the reply.

# 4. (Optional) Enable triage later
$EDITOR ops/gateway.yaml                    # set triage: openrouter
echo OPENROUTER_API_KEY=sk-or-... >> .env
jc gateway reload
```

### Switching triage from OpenRouter to a direct provider

OpenRouter remains supported, but the gateway can also call providers directly
through the generic `api_classifier` backend. For OpenAI-compatible providers
such as DeepSeek, Groq, Together, Fireworks, or Cerebras:

```yaml
triage: api_classifier
triage_protocol: openai_compat
triage_base_url: https://api.deepseek.com/v1
triage_api_key_env: DEEPSEEK_API_KEY
triage_model: deepseek-chat
triage_timeout_seconds: 5
triage_max_tokens: 200
```

For Anthropic's Messages API:

```yaml
triage: api_classifier
triage_protocol: anthropic
triage_base_url: https://api.anthropic.com/v1
triage_api_key_env: ANTHROPIC_API_KEY
triage_model: claude-haiku-4-5
triage_timeout_seconds: 5
triage_max_tokens: 200
```

Store the matching key in `.env`, then run `jc doctor` and `jc gateway reload`.
`jc doctor` reports the configured protocol, provider host, model, and whether
the configured key env var resolves.

## What the release hook does

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

The legacy live-Claude path remains deprecated. Use `jc update --instance-dir`
to apply the gateway release hook, then restart the gateway/watchdog.

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
