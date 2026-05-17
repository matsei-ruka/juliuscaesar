# Spec: Supervisor — per-event progress narration and silent recovery

**Status:** Proposed
**Date:** 2026-05-17
**Branch base:** `main`
**Owner:** tbd

---

## 1. Goal

Give the user a meaningful, contextual progress signal while a long brain
invocation is still running, and silently recover when the adapter crashes —
without ever exposing crash state to the conversation.

The supervisor is the missing complement to the intelligent watchdog
(`docs/specs/intelligent-watchdog.md`). The intelligent watchdog observes
brain health and refuses to send static progress filler. The supervisor
produces real progress content via a deterministic phase signal plus a cheap
AI narrator, and owns the silent-recovery side that the watchdog explicitly
leaves to gateway recovery.

The supervisor must:

1. detect when a running event has been processing long enough to warrant a
   user-visible card (per-brain threshold, default 60s);
2. classify the current phase from deterministic signals (adapter PID alive,
   stderr tail keywords, tool-use trace, worker linkage);
3. ask a cheap model to narrate the last meaningful signal in the request's
   language;
4. render a single contextual-emoji card to the originating conversation,
   updated in place via channel-specific edit semantics;
5. on adapter failure (non-zero exit, stderr fatal, PID dead before any
   output), recover the event silently — never expose `crash` to chat;
6. never write to `state/transcripts/` (loop guard — critical).

---

## 2. Current State

Today the user sees nothing between sending a message and receiving a reply.
The gateway:

- claims the event (`status='running'`, sets `started_at`, applies a lease
  via `locked_until`);
- spawns the adapter (`lib/gateway/brains/base.py:invoke()` writes
  `state/gateway/adapter_stderr/<event>-<pid>-<ts>.log`);
- logs `adapter spawn event=… brain=… pid=… …`;
- on adapter failure routes through `RecoveryIntegration` → operator alert.

The intelligent watchdog (v1, shipped) observes long-running rows but
deliberately does not send chat progress messages. Static filler is worse
than silence; the watchdog has no task context.

The supervisor closes that gap by combining real task context (stderr tail,
tool-use trace, worker linkage) with a cheap narrator model.

Recovery today: gateway calls `RecoveryIntegration.classify()` on
`AdapterFailure` and the event lands in `failed` after `retry_count >= max`.
Failed events stay failed. There is no automatic session-poison detection or
silent-replay path. Mikaela image_url crash (2026-05-17) needed manual sqlite
reset of event 80 to recover; the supervisor formalizes that flow.

---

## 3. Non-Goals

- Not replacing the intelligent watchdog. The watchdog stays the source of
  truth for brain-level health (auth expiry, repeated runtime failures,
  brain cooldowns). The supervisor is per-event and observation-tight.
- Not killing the adapter at the timeout mark. The supervisor narrates, it
  does not interrupt. Lease expiry remains the gateway's mechanism.
- Not posting in groups by default. Group chats opt in via config to avoid
  noise (see §11).
- Not narrating worker-linked events. If the brain has spawned a worker
  (`jc workers list` shows a linked worker for this conversation_id), the
  supervisor backs off — the worker has its own progress channel.
- Not exposing internal phase names ("scanning files") for events whose
  source is a group chat or where the operator has disabled the supervisor.
- Not writing to `state/transcripts/`. Ever. The supervisor's output is
  channel-specific (Telegram message edit, etc.) and never persists into
  the conversation transcript that the brain reads next turn.
- Not asking the stuck brain about its own state.
- Not retrying brain-classifier errors (e.g. provider 5xx) — that remains
  gateway recovery's responsibility.

---

## 4. Concepts

### 4.1 Supervisor Tick

Cron-driven, stateless tick. Mirrors `lib/watchdog/watchdog.sh` cadence.
Runs every 30s by default (configurable). Each tick:

1. read all `running` events from `state/gateway/queue.db` whose
   `(now - started_at) >= notice_threshold`;
2. for each: skip if a worker is linked, or if the event source channel is
   excluded by config, or if a card has been rendered within
   `min_card_interval_seconds`;
3. build snapshot (adapter PID alive, stderr tail, mtime, prior cards);
4. classify phase (deterministic) and select emoji;
5. invoke narrator model (one call per event per tick, max
   `narrator_calls_per_tick`);
6. render card via channel adapter; record state in
   `state/supervisor/state.json`.

A separate "completion" pass on each tick reconciles cards: if an event has
moved to `done` since the last tick, replace the running card with a final
emoji (✅) for one render, then stop tracking. If `failed`, trigger silent
recovery (§9) and clear the card.

### 4.2 Daemon vs cron

Cron tick is the v1 choice. Reasons:

- Stateless: tick crash does not lose state (state.json on disk).
- Mirrors the watchdog pattern operators already understand.
- Easy to disable (remove crontab line).
- No long-lived process to monitor.

A v2 daemon may make sense if narrator latency dominates and we want to
push cards faster (sub-second after a phase change). Out of scope for v1.

### 4.3 Relationship to intelligent watchdog

| Concern | Owner |
|---|---|
| Brain health classification (auth, runtime, cooldown) | intelligent watchdog |
| Per-event progress narration to user | **supervisor** |
| Event lease/timeout (`locked_until` expiry) | gateway queue |
| Adapter-failure classification & retry | gateway `RecoveryIntegration` |
| Session-poison detection & drop | brain `adjust_resume_session()` hook |
| Silent recovery of poisoned/crashed events | **supervisor** |
| Operator-visible health/auth alerts | intelligent watchdog |

The two systems share `state/gateway/queue.db` and log files but use
separate state directories. They never write to each other's state.

---

## 5. Snapshot Inputs

Each supervisor tick builds a bounded snapshot per running event. Costs
must be bounded by `O(events × constant)` — no whole-log scans.

### 5.1 Queue Snapshot

From `state/gateway/queue.db`:

- `id, status, started_at, received_at, source, conversation_id, user_id`;
- decoded `meta` for delivery (chat_id, message_thread_id, etc.).

Filter:

- `status = 'running'`;
- `started_at IS NOT NULL`;
- `(now - started_at) >= notice_threshold_seconds[brain]`.

Skip if worker is linked (see §5.4).

### 5.2 Adapter Stderr Snapshot

`state/gateway/adapter_stderr/<event_id>-<pid>-<ts>.log`. Tail last
`stderr_tail_bytes` (default 4096) without locking. Track `mtime`.

Heuristic signals:

- `mtime` within last 5s → "active" (full activity bar);
- `mtime` 5–30s → "warm" (partial bar, decay linear);
- `mtime` >30s → "idle" (empty bar — candidate stall);
- file missing or empty → "starting" (adapter spawned but no output yet).

### 5.3 Adapter PID Snapshot

Parse `adapter spawn event=<id> … pid=<pid>` from latest gateway log line
for this event. Check `/proc/<pid>` (Linux) or `ps -p <pid>` (cross-platform
fallback) for liveness. If PID gone before `done`/`failed` in queue: this
is the **crash before-exit-write** path — trigger silent recovery.

### 5.4 Worker Linkage Snapshot

`jc workers list --json` (or read `state/workers/index.json` directly).
Skip narration if a worker for the same `conversation_id` is `running` —
the worker has its own progress channel, supervisor would be double signal.

### 5.5 Prior Cards Snapshot

`state/supervisor/state.json` records, per event:

```json
{
  "events": {
    "1234": {
      "first_card_at": "2026-05-17T14:00:30Z",
      "last_card_at":  "2026-05-17T14:01:30Z",
      "last_phase":    "scanning",
      "channel_message_id": 7572,
      "narration_count": 2,
      "language": "it"
    }
  }
}
```

Atomic write (`.tmp` + rename). Used for:

- card edit (re-use `channel_message_id`);
- backoff (`min_card_interval_seconds`);
- phase-change detection (only re-narrate if phase changed or fresh signal).

---

## 6. Phase Classifier

Deterministic. No LLM. Reads stderr tail + tool-use trace.

### 6.1 Phase → Emoji Map

| Phase | Emoji | Trigger keywords / signals |
|---|---|---|
| `starting` | 🟢 | event in `running` but no stderr output yet |
| `thinking` | 💭 | adapter spawned, stderr quiet, no tool-use yet (>10s) |
| `reading` | 📖 | tool-use `Read`/`Glob` events in stderr |
| `searching` | 🔍 | tool-use `Grep`/`WebSearch`/skill calls |
| `web_research` | 🌐 | tool-use `WebFetch`/Brave/Tavily/Firecrawl |
| `coding` | 🛠️ | tool-use `Edit`/`Write`/`Bash` |
| `analyzing` | 🧮 | long stretch of text output, no tool-use |
| `calling_api` | 📞 | adapter waiting on external API (provider request open >5s) |
| `packaging` | 📦 | tool-use `executive-deck`/`infographics-creator` |
| `delivering` | 🎯 | stderr matches `final answer`/`response complete`/adapter near exit |
| `idle` | ⏸️ | stderr mtime > 30s, PID alive, no progress |
| `done` | ✅ | event moved to `status='done'` (one final render) |

`crash` state has no emoji in the user-facing card. It triggers silent
recovery (§9). Internal supervisor log uses `💥` for operator visibility
only.

The classifier matches keywords from a YAML table (`lib/supervisor/phases.yaml`)
shipped with the framework; operators may override per brain.

### 6.2 Selection Rules

- Multiple matches → most recent in stderr tail wins.
- No matches → `thinking` (default).
- Phase persists across ticks until a different match appears (avoids
  flicker when stderr is briefly quiet).

---

## 7. AI Narrator

A cheap model produces one short sentence: "Ultimo segnale" / "Last signal".

### 7.1 Model

Default: `openrouter:deepseek-v4-flash` (matches existing triage cost
profile, ~$0.0001/call). Operator-overridable via
`supervisor.narrator_brain` in gateway.yaml. Recommended cheap options:
deepseek-v4-flash, claude:haiku, openai/gpt-4-mini.

### 7.2 Prompt

System prompt embeds:

- request language (mirror — IT in → IT out);
- selected phase;
- stderr tail (last 2KB, redacted — see §11);
- prior narration (avoid repeat).

User prompt: "Narra in una frase l'ultimo segnale meaningful nell'output.
Niente filler. Niente metaframework. Max 14 parole."

Output schema:

```json
{ "narration": "trovati 184 file PHP, sto cercando controllers critici" }
```

Validation: <= 140 chars, no banned tokens (see §11), no language drift
(matches request language). On schema fail: use phase label as fallback
("scanning files" / "scansione file in corso").

### 7.3 Cost Guard

`supervisor.narrator_calls_per_tick_max` (default 5). If more events
qualify, narrate the N oldest, fallback others to phase-label-only cards.

`supervisor.narrator_calls_per_event_max` (default 6). Caps narration spend
per single long event.

---

## 8. Card Format

### 8.1 Structure

```
<emoji> <title>

Fase: <phase_label>
Attività: <activity_bar>  <freshness_note>
Ultimo segnale: <narration>
Tempo: <elapsed_mmss>
```

- `<title>` = first 60 chars of the user's request, truncated on word
  boundary. Pulled from `meta.text` or `meta.transcription`.
- `<phase_label>` = i18n phase string (table in `phases.yaml`).
- `<activity_bar>` = 10 unicode blocks: `█` for active 1s blocks, `░` for
  decayed. Driven by stderr mtime.
- `<freshness_note>` = `ultimo output 8s fa` / `last output 8s ago`.
- `<narration>` = narrator output, or phase label fallback.
- `<elapsed_mmss>` = `MM:SS` since `started_at`.

### 8.2 Language Mirror

The card renders in the language of the originating user message. Detection
rules (in order):

1. `meta.language` if present (some channels supply it);
2. fasttext/langdetect on `meta.text`;
3. fallback to instance default (`gateway.default_language` or `en`).

The narrator inherits this language. The phase table is multilingual;
unknown phase keys fall back to English.

### 8.3 Channel Rendering

**Telegram (primary):** first card sent via `sendMessage` (records
`channel_message_id`). Subsequent updates via `editMessageText` on the
same message_id. Use code block (```` ``` ````) for the activity bar to
preserve monospace alignment. MarkdownV2 escape applies. Final ✅ card
edits the same message; no separate "done" message.

**Slack:** initial `chat.postMessage`, updates via `chat.update`.

**Discord:** initial message, updates via `editMessage`.

**Voice / cron / jc-events / email:** **no card.** These channels have no
streaming surface; the user gets only the final reply when the brain
finishes. Recovery still applies silently.

### 8.4 Cadence

- First card after `notice_threshold_seconds` per brain (defaults: claude
  30s, codex 90s, pi 45s, openrouter 30s).
- Edit cadence: `min_card_interval_seconds` (default 15s). Skip edit if
  phase + narration unchanged.
- Max cards per event: `max_cards_per_event` (default 12; ~3min of edits
  at 15s cadence beyond first card).

---

## 9. Silent Recovery Ladder

Triggered when supervisor detects one of:

1. **Crash before exit-write** — adapter PID gone, queue still `running`,
   no `adapter exit` log line. Gateway crashed mid-invoke.
2. **Session poison** — adapter exited non-zero, stderr matches
   `unknown variant 'image_url'` or other known session-poison patterns
   (table in `lib/supervisor/recovery_patterns.yaml`).
3. **Adapter segfault** — non-zero exit, no readable stderr, PID killed by
   signal (rc < 0 or rc >= 128).
4. **Provider 5xx** — stderr matches `5\d{2}` HTTP status, transient.

### 9.1 Ladder per failure class

| Class | Action 1 | Action 2 (if Action 1 fails) | Action 3 (escalate) |
|---|---|---|---|
| Crash before exit-write | Reset event to `queued`, retry_count=0, available_at=now | Same, with `--reset-resume` (drop JC_RESUME_SESSION) | Operator alert via watchdog |
| Session poison | Reset + drop session UUID from `meta.resume_session` | Switch to fallback brain (gateway.yaml `recovery.fallback_brain`) | Operator alert |
| Adapter segfault | Reset event, retry_count=0 | Reset + fallback brain | Operator alert |
| Provider 5xx | Reset event, backoff 30s | Reset + retry (max 3) | Hand off to recovery, operator alert |

### 9.2 Recovery Mechanism

Supervisor writes the reset via the same code path the manual reset uses
today — a single method on `lib/gateway/queue.py`:

```python
def reset_running_to_queued(
    event_id: int,
    *,
    drop_resume_session: bool = False,
    available_in_seconds: int = 0,
) -> None
```

This wraps the sqlite UPDATE that previously had to be issued by hand.

### 9.3 Loop Guard

Per event, supervisor records `recovery_attempts` in `state/supervisor/state.json`.
After `max_recovery_attempts` (default 2), supervisor stops auto-recovery,
hands the event to gateway recovery (which sends operator alert via
intelligent watchdog), and the event lands in `failed`.

### 9.4 No User-Visible Crash

The user never sees `💥`, "errore", "crash", "retry", or any framework
internals. Card stays at last known state until reset succeeds, then
returns to `🟢 starting` on the new attempt. If recovery fails terminally,
the existing intelligent-watchdog operator-alert path takes over (group
chats receive only the generic "I notified the operator" message).

---

## 10. Loop Guard — Critical

The supervisor produces text that goes to the user but **must never** be
written to `state/transcripts/<conversation_id>.jsonl`. Reasons:

- Transcripts are the brain's memory of prior turns. Including supervisor
  cards there would make the next brain invocation think it said "scanning
  files" itself.
- Supervisor cards are ephemeral state, not conversational content.
- A brain reading its own supervisor narration would loop on style and
  produce meta-output about its own progress.

Implementation: supervisor sends via channel adapters' low-level send/edit
APIs (`channel.client.send_message` / `client.edit_message`) directly,
bypassing `lib/gateway/dispatch.py:deliver_response()` which writes to
transcripts. A separate `deliver_supervisor_card()` helper enforces this
boundary.

Tests: integration test asserts that running the supervisor against a
running event produces no new lines in `state/transcripts/<conv>.jsonl`.

---

## 11. Safety Rules

- **No transcript writes** (§10, hard rule).
- **No secrets in cards.** Stderr tail must be redacted: regex strip
  `(?i)(api[_-]?key|token|secret|authorization|bearer)\s*[:=]\s*\S+`
  before passing to narrator and before writing to supervisor.log.
- **No internal phase names in groups.** When the originating chat is a
  group, render phase label as a generic verb ("lavorando" / "working")
  unless `supervisor.groups.show_phase` is explicitly `true` for that
  chat.
- **No framework references in narration.** Banned narration tokens:
  `gateway`, `adapter`, `brain`, `JuliusCaesar`, `conversation_id`,
  `event_id`, `pid`, `stderr`, `queue.db`. Narrator system prompt enforces;
  validation drops outputs that contain them.
- **No crash exposure.** § 9.4.
- **Backoff respects user inactivity.** If the user sends a new message
  while supervisor is mid-card (event still `running`), the supervisor
  stops editing — the next adapter invoke will replace context anyway.
- **Quiet hours respect.** Supervisor inherits `gateway.quiet_hours` per
  channel — no cards during operator-defined quiet windows.

---

## 12. Configuration

`ops/gateway.yaml` additions:

```yaml
supervisor:
  enabled: true
  tick_interval_seconds: 30
  notice_threshold_seconds:
    claude: 30
    codex: 90
    pi: 45
    openrouter: 30
    default: 60
  min_card_interval_seconds: 15
  max_cards_per_event: 12
  narrator_brain: openrouter:deepseek-v4-flash
  narrator_calls_per_tick_max: 5
  narrator_calls_per_event_max: 6
  stderr_tail_bytes: 4096
  channels:
    telegram: true
    slack: true
    discord: true
    voice: false
    cron: false
    jc-events: false
    email: false
  groups:
    enabled: false           # default: no cards in group chats
    show_phase: false        # generic verb instead of phase name
  recovery:
    enabled: true
    max_recovery_attempts: 2
    fallback_brain: null     # optional brain to switch to on session-poison
    backoff_seconds: 30
  phases_table: lib/supervisor/phases.yaml
  recovery_patterns: lib/supervisor/recovery_patterns.yaml
```

CLI:

```bash
jc supervisor status [--json]
jc supervisor disable
jc supervisor enable
jc supervisor reset <event_id>     # clear card state for one event
```

Cron line (`crontab -l`):

```cron
* * * * * /home/<user>/.local/bin/jc-supervisor tick >> /var/log/jc-supervisor.log 2>&1
```

Self-throttles to `tick_interval_seconds` internally; cron fires every
minute, tick noops if last tick was < interval ago.

---

## 13. Observability

Structured log kinds (gateway `state/logs/supervisor.jsonl`):

- `supervisor_tick_begin` / `supervisor_tick_end`
- `supervisor_event_snapshot`
- `supervisor_phase_classified`
- `supervisor_narrator_call` (model, latency, cost)
- `supervisor_card_rendered` (channel, message_id, edit/new)
- `supervisor_card_skipped` (reason: worker_linked, backoff, etc.)
- `supervisor_recovery_triggered` (class, action)
- `supervisor_recovery_failed`
- `supervisor_loop_guard_violation` (must be zero in prod)

Metrics:

- cards_per_tick (avg, p95)
- narrator_latency_ms (p50/p95)
- recovery_attempts_per_event (histogram)
- ticks_with_zero_qualifying_events (mostly idle instance signal)

---

## 14. Implementation Plan

### Phase 1 — Snapshot + Phase Classification (no user output)

- `lib/supervisor/__init__.py`
- `lib/supervisor/snapshot.py`
- `lib/supervisor/phases.py` + `phases.yaml`
- `lib/supervisor/runner.py` (tick orchestrator)
- `bin/jc-supervisor` (cron entry)
- `jc supervisor status --json`
- Snapshot only; classify phase; log to supervisor.jsonl. **No cards.**

### Phase 2 — Card Renderer + Telegram

- `lib/supervisor/cards.py` (card template + i18n)
- `lib/supervisor/delivery.py` (channel send/edit, no-transcript guarantee)
- Telegram integration; activity bar from stderr mtime; first card after
  threshold.
- Skip group chats by default.

### Phase 3 — Narrator

- `lib/supervisor/narrator.py` (model call, validation, redaction)
- Wire deepseek-v4-flash via openrouter brain.
- Cost guards + cap.

### Phase 4 — Slack + Discord cards

- Same renderer, channel-specific edit semantics.

### Phase 5 — Silent Recovery

- `lib/gateway/queue.py:reset_running_to_queued()`
- `lib/supervisor/recovery.py` + `recovery_patterns.yaml`
- Crash before-exit-write detection (PID alive check).
- Session-poison pattern matcher.
- Loop guard (max_recovery_attempts).

### Phase 6 — Watchdog Handoff

- After max_recovery_attempts, hand off to intelligent watchdog
  (event lands in `failed`, watchdog issues operator alert).
- Ensure no double-alert (supervisor.log records hand-off, watchdog
  inspects supervisor state before re-alerting).

---

## 15. Test Plan

### Unit

- Snapshot reads only `running` events past threshold per brain.
- Phase classifier picks most recent match, defaults to `thinking`.
- Activity bar decay matches mtime curve.
- Card template renders MarkdownV2-safe Telegram output.
- Language mirror: IT request → IT card; EN → EN.
- Narrator validator drops banned tokens; falls back to phase label.
- Redaction strips api_key/token/secret from stderr tail.
- `reset_running_to_queued()` resets row + optionally drops resume_session.

### Integration

- Running event past threshold renders Telegram card.
- Card edits in place on phase change (single message_id).
- Worker linkage skips card.
- Event moving to `done` produces ✅ final edit, then no more cards.
- Adapter PID dies mid-invoke → supervisor resets event silently, user
  sees continuation only.
- Session-poison stderr (`unknown variant 'image_url'`) → reset + drop
  resume_session; new attempt succeeds.
- Group chat: no card by default; with `groups.enabled: true`, card uses
  generic verb (no phase name leak).
- Quiet hours: no cards emitted during window.
- **Loop guard:** running supervisor against a running event produces zero
  new lines in `state/transcripts/<conv>.jsonl`. This test must run on
  every PR.

### Manual smoke (Mikaela, pi brain)

1. Send a request that triggers a long pi invocation (image analysis).
2. After 45s confirm Telegram card appears with phase `analyzing` or
   `searching` and a narration line in request language.
3. Card edits in place every 15s.
4. Final reply replaces card with ✅.
5. Inject a known session-poison event (image_url in resumed pi session) —
   confirm supervisor resets silently; user only sees the eventual reply,
   no crash text.

---

## 16. Open Questions

1. **Edit vs append on Telegram.** Edit is cleaner UX but loses card
   history. Default: edit. Consider config flag for users who prefer
   append.
2. **Should the supervisor narrator share `gateway.yaml triage_routing`**
   or have its own brain config? Current spec: separate
   (`supervisor.narrator_brain`) for clarity.
3. **Phase table localization.** v1 ships EN + IT. Adding more languages
   means more YAML entries — should we accept fallbacks to EN per phrase,
   or require full translation per language?
4. **Cron vs systemd-timer.** Cron is universal but coarser; systemd
   timer gives sub-minute precision. v1 defaults to cron with internal
   throttle; v2 can offer either.
5. **Recovery audit trail.** Should silent recovery write to a dedicated
   `state/supervisor/recoveries.jsonl` for operator review, separate from
   the per-tick log? Tentatively yes — cheap, useful for postmortems.
