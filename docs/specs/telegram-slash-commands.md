# Telegram slash commands spec

Slash commands are handled locally by the TelegramChannel, not enqueued to the brain. They provide control and observability without round-tripping through the gateway.

## Supported commands

### `/help`
List available commands.

Response:
```
/models — show available model routing
/compact — trigger context compaction
/help — this message
```

### `/models`
Show the brain's model routing table for this instance.

Reads from `ops/gateway.yaml:triage_routing` and displays:
```
smalltalk ↦ claude:haiku
quick ↦ claude:sonnet
analysis ↦ claude:sonnet
code ↦ claude:opus
image ↦ claude:sonnet
voice ↦ claude:sonnet
system ↦ claude:opus
```

Also shows:
- default_brain
- default_fallback_brain
- sticky_brain_idle_timeout_seconds

### `/compact`
Request context compaction on the live session.

Writes a file to `state/signals/compact` (similar to heartbeat signal pattern). The watchdog or next Claude session detects this and runs `claude --compact` or calls the `/compact` HTTP endpoint.

Response:
```
✓ Compaction request queued. Next response will include current context metrics.
```

## Implementation

- Detect slash commands in `TelegramChannel.run()` before enqueueing.
- Extract command name and args from `message.text`.
- Route to handler method `_handle_command_<name>`.
- Use `send_text()` to reply directly.
- Log command execution.
- Non-existent commands → `/help`.

## Error handling

- If `ops/gateway.yaml` is missing → respond with error
- If signal write fails → log and respond with error
- Invalid syntax → show usage
