# Conversation Transcripts (per-conversation logging)

## Problem

When a Claude session resumes after expiry, context is lost. Operator can grep queue.db events but only gets inbound messages — no assistant responses. No unified transcript per conversation across channels.

For multi-channel users (e.g., Sergio in both DM + group), finding all messages requires cross-channel search.

## Solution

Log full conversation (inbound + assistant response) to a per-conversation file. Format: JSONL (one event per line). Append-only. Load on session resume for priming context.

## Design

### File structure
```
state/transcripts/{conversation_id}.jsonl
```

Each line:
```json
{
  "ts": "2026-04-29T10:32:48Z",
  "role": "user|assistant",
  "text": "message body",
  "message_id": "12345",
  "channel": "telegram",
  "chat_id": "28547271"
}
```

### Integration points

1. **Inbound (gateway)**: when message enqueued, append line with `role=user`.
2. **Outbound (gateway)**: after adapter returns response, append line with `role=assistant`.
3. **Session resume**: `--resume` flow reads last N lines from transcript file, prepends to context prompt.
4. **Archival**: transcripts stay in `state/` (not moved/cleaned); heartbeat optionally compresses old transcripts.

### Grep use case

Find all messages involving Sergio across all conversations:
```bash
grep -r "sergio" state/transcripts/ | jq -r '.text' | head -20
```

Find all messages in conversation_id 28547271 on 2026-04-29:
```bash
grep "2026-04-29" state/transcripts/28547271.jsonl
```

### Scope

- Per-conversation only (not global log). Keeps files bounded.
- Inbound + assistant response only. Not intermediate tool calls or system messages.
- Optional: heartbeat task compresses transcripts older than N days to `.gz`.

### Out of scope

- Full-text search (use grep + jq; if needed later, add SQLite mirror)
- Cross-session merge (start fresh per conversation_id each session)
- Reattribution on role confusion (assistant always matches brain name)

## Implementation

1. Add `transcripts_dir` to InstanceConfig (default: `state/transcripts`).
2. Add append function: `log_to_transcript(conversation_id, role, text, message_id, channel)`.
3. Hook on event enqueue + response return in gateway.
4. On `--resume`, check transcript file; if exists, read last 5-10 lines as priming context.
5. Tests: verify JSONL format, verify append-only, verify resume loads context.

## Agent doctrine — how to use transcripts at runtime

Transcripts are the agent's long-term memory of every chat thread. Every JC instance loads this doctrine into L1 RULES.md so agents know when + how to consult them.

### When to consult transcripts

1. **User references past conversation**: "remember when we talked about X", "the message I sent yesterday", "what did Sergio say". Agent must check transcript before answering, not guess.
2. **Cross-channel context**: same user mentioned in another chat. Grep all transcripts for username/handle to find related context.
3. **Resume after long gap**: when conversation_id last_seen > 24h, read tail of transcript on first inbound to refresh context.
4. **Disambiguation**: when "the project" or "that idea" is referenced and current session has no anchor — search transcripts for last mention.

### How to query

**CLI tools:**
```bash
# Read full conversation transcript
jc transcripts read <conversation_id>

# Read last N events from a conversation (defaults to 20)
jc transcripts tail <conversation_id> [--lines N]

# Find messages mentioning text across all conversations
jc transcripts search "<query>" [--user <username>] [--since <date>]

# Find specific message by id
jc transcripts get <message_id>
```

**Direct file access** (when CLI not available):
```bash
# Quick grep
grep -r "luca said" state/transcripts/*.jsonl

# Last 10 lines of a conversation
tail -n 10 state/transcripts/28547271.jsonl | jq -r '"\(.ts) [\(.role)] \(.text)"'
```

### How to use what you find

- **Prefer recent over old.** Transcripts are time-ordered — last N lines usually carry the live context.
- **Cite, don't paraphrase.** When user asks "what did I say about X", quote the matching line + ts. Builds trust.
- **Combine with L2 memory.** Transcripts = raw chat. L2 = curated facts. Use both: grep transcript first, then check L2 for the structured version.
- **Don't dump full history.** Load only what answers the user's question. Loading 200 lines for a single fact wastes context.

### Workflow examples

**User: "what did Sergio say last week about the API?"**
```bash
jc transcripts search "API" --user sergio --since 2026-04-22
# → returns 3 matching messages across 2 conversations
# Agent reads, summarizes, cites ts + chat
```

**User: "remember the proposal from yesterday?"**
```bash
jc transcripts tail <conversation_id> --lines 50 --since 2026-04-28
# Agent scans for "proposal" or related terms, picks most relevant
```

**Resume after 3 days of silence:**
```bash
# Auto-injected on session resume — no agent action needed
# But if context still feels thin, agent can pull more:
jc transcripts tail <conversation_id> --lines 30
```

### Anti-patterns

- Don't grep transcripts on every turn. Cache or rely on session memory for active threads.
- Don't load other users' transcripts without need — privacy + token cost.
- Don't write back to transcripts directly. Append-only by gateway, not agent.
- Don't trust assistant lines as ground truth — they're past predictions, not facts.

## Future

- Transcript rotation (e.g., 100MB per file, auto-split).
- Compression scheduler (daily task gzip old transcripts).
- Embeddings index for semantic search (vs. keyword grep).
- Cross-channel correlation: link conversations by user_id across telegram/slack/discord.
