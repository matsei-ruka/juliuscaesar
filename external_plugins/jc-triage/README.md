# jc-triage

Minimal Claude Code plugin that exposes a `/classify` HTTP endpoint backed by a
long-running Haiku session. The gateway's `claude-channel` triage backend POSTs
to this endpoint and parses the returned single-line JSON.

## Status

This is a reference scaffold for users who want to keep triage on their Claude
subscription instead of paying OpenRouter or running ollama locally. The
plugin is not strictly required: pick `triage: ollama` or `triage: openrouter`
in `ops/gateway.yaml` to skip it entirely.

## How it works

1. The gateway starts and reads `triage: claude-channel` from `ops/gateway.yaml`.
2. A separate Claude session runs in a screen with this plugin loaded:

   ```
   screen -dmS jc-triage \
     claude --model claude-haiku-4-5 \
            --channels plugin:jc-triage@local \
            --dangerously-skip-permissions
   ```

3. The plugin opens an HTTP listener on `${TRIAGE_PORT}` (default 9876).
4. On `POST /classify {"message":"..."}`, the plugin emits a channel
   notification containing the message + system prompt; the host Haiku
   session responds via the plugin's `reply` tool, which the plugin returns
   over HTTP as `{"text":"..."}`.

## Files

| File           | Purpose                                                 |
|----------------|---------------------------------------------------------|
| `server.ts`    | Bun-runnable plugin entry point                         |
| `package.json` | Bun manifest                                            |
| `tsconfig.json`| TypeScript config                                       |

## Install

```
cp -r external_plugins/jc-triage ~/.claude/plugins/jc-triage
cd ~/.claude/plugins/jc-triage && bun install
```

Then restart the gateway with `claude-channel` triage enabled.

## Caveats

- Adds ~1.5–3s latency per classification (extra Claude cold-start).
- A second Claude process runs continuously — modest memory cost.
- Not free for non-Claude users; use ollama or openrouter instead.
