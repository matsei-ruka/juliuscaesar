# Spec: Intelligent watchdog for brain health and long-running requests

**Status:** Implemented v1
**Date:** 2026-05-12
**Branch base:** `main`
**Owner:** tbd

---

## 1. Goal

Upgrade the watchdog from "is the gateway process alive?" to "is the assistant
still able to answer the user?"

The gateway can be up while the active brain is broken, logged out, wedged on a
long task, or repeatedly timing out. In those cases the watchdog must:

1. inspect gateway state and logs;
2. classify clear runtime/auth failures with deterministic evidence;
3. notify the right chat only for user-visible brain/auth failures;
4. observe long-running work without sending generic progress messages;
5. avoid duplicate alerts and avoid creating retry loops.

This spec covers the product and implementation contract. v1 is implemented in
`lib/watchdog/intelligence/` and wired into both watchdog v2 and the legacy
gateway-mode bash watchdog.

---

## 2. Current State

Today the gateway-mode watchdog is mostly process supervision:

- `lib/watchdog/watchdog.sh:main_gateway()` checks the gateway pidfile and
  restarts the gateway if the daemon is down.
- `lib/watchdog/supervisor.py` v2 can supervise configured children via
  pid/heartbeat checks, restart budgets, and operator alerts.
- `lib/gateway/runtime.py` writes a heartbeat from a background thread, so a
  long adapter invocation does not make the daemon look dead.
- `lib/gateway/queue.py` marks a claimed event `running`, sets
  `started_at`, and applies a lease through `locked_until`.
- Adapter failures already flow into `RecoveryIntegration`, which classifies
  failures such as `session_expired`, `session_missing`, `transient`, and
  `bad_input`.

The v1 implementation closes the original product gap by adding:

- Recent gateway JSON logs are inspected for brain/auth errors.
- `events.status='running'` rows are inspected to detect a user request that is
  taking too long.
- Failed/past requests are left to gateway recovery and operator action; watchdog
  does not replay or switch them.
- Long-running requests are recorded for operator visibility, but watchdog does
  not send chat progress messages for them.

---

## 3. Non-Goals

- Not replacing gateway retry/recovery. Existing adapter failure recovery stays
  in the gateway and remains the first responder after `invoke_brain()` returns
  or raises.
- Not killing long-running brain subprocesses at the three-minute mark. The
  watchdog observes them; human progress messages belong in the active
  brain/runtime flow where real task context exists.
- Not moving failed/past user messages to another brain. Gateway recovery owns
  retry/replay after adapter failure.
- Not sending status messages through the stuck brain. Watchdog status delivery
  uses the channel delivery layer directly.
- Not changing normal triage routing for healthy requests.

---

## 4. Concepts

### 4.1 Intelligent Watchdog Tick

Add a new watchdog phase after gateway liveness passes:

```text
gateway alive?
  no  -> existing restart path
  yes -> intelligent watchdog tick
           inspect queue
           inspect gateway logs
           classify clear runtime/auth failures deterministically
           notify/mark brain health only for active evidence
```

This phase should live in Python, not in `watchdog.sh`. Preferred placement:

```text
lib/watchdog/intelligence/
  __init__.py
  snapshot.py       # queue/log/config snapshot
  evaluator.py      # deterministic classifier
  decisions.py      # dataclasses for decisions
  actions.py        # notify, mark state
  state.py          # dedupe/cooldown state
```

`lib/watchdog/supervisor.py` should call the intelligent tick for the gateway
child once the gateway heartbeat is healthy. The older bash `main_gateway()`
may shell out to a new `jc-watchdog brain-health` helper as a compatibility
bridge, but the long-term path is the v2 supervisor.

### 4.2 Watchdog State

Persist dedupe/cooldown state under:

```text
<instance>/state/watchdog/intelligence.json
```

Minimum fields:

```json
{
  "notified_events": {
    "123": {
      "brain_issue_notice_at": "2026-05-12T10:05:00Z"
    }
  },
  "brain_health": {
    "claude": {
      "state": "healthy|suspect|unavailable",
      "reason": "session_expired",
      "until": "2026-05-12T10:30:00Z"
    }
  }
}
```

State writes must be atomic (`.tmp` + rename). The watchdog may run every
minute, so duplicate prevention is load-bearing.

---

## 5. Snapshot Inputs

Each intelligent tick builds a bounded snapshot. It must not dump whole logs or
full prompts into watchdog decision state.

### 5.1 Queue Snapshot

Read from `state/gateway/queue.db`:

- running events where `started_at` or `received_at` is older than
  `watchdog.long_running_notice_seconds`;
- `source`, `conversation_id`, `user_id`, and decoded `meta` for delivery.

For long-running detection, use `started_at` when present; fall back to
`received_at`. Default threshold:

```yaml
watchdog:
  long_running_notice_seconds: 180
```

Terminal `failed` events are not watchdog recovery candidates. The watchdog must
not scan failed rows looking for missed messages, must not patch
`event.meta.brain_override`, must not add `event.meta.watchdog_switch`, and must
not call `queue.retry_now()`. Failed adapter handling remains owned by gateway
recovery.

### 5.2 Gateway Log Snapshot

Read the current gateway JSON log and extract only recent relevant records:

- `dispatch begin`;
- `dispatch failed`;
- `adapter timeout`;
- `recovery classify`;
- `recovery fail`;
- `recovery defer`;
- `triage error`;
- `event failed`;
- auth/session keywords from stderr previews.

Bound the window by both count and age:

```yaml
watchdog:
  log_window_seconds: 900
  log_window_lines: 200
```

The evaluator receives summarized log entries, not raw unbounded log files.

### 5.3 Config Snapshot

Include:

- selected brain/model for the event when known;
- delivery channel availability.

---

## 6. Deterministic Watchdog Evaluator

The watchdog evaluator must not call the configured triage provider/model. Normal
gateway triage still routes incoming user messages, but watchdog health handling
is a separate deterministic subsystem. It reads bounded queue/log evidence and
returns a `Decision` directly from code.

Required output schema:

```json
{
  "kind": "healthy|brain_unhealthy|auth_expired|long_running|transient_slow|unknown",
  "confidence": 0.0,
  "severity": "info|warning|critical",
  "user_visible": true,
  "should_switch_brain": false,
  "summary": "short operator-safe reason"
}
```

Rules:

- `auth_expired`: login/session/key failure is likely. User-visible
  notification required. Brain should be marked unavailable until recovery or
  cooldown expiry.
- `brain_unhealthy`: current brain is crashing/timing out/rejecting before it
  can answer. User-visible notification required if tied to a user event.
  The brain may be marked temporarily unavailable; the event is not replayed or
  switched by watchdog.
- `long_running`: a running event exceeded the notice threshold but there is no
  clear failure. Record the decision for visibility; do not send a progress
  message and do not switch.
- `transient_slow`: network/provider slowness or one-off timeout. User-visible
  notification is not handled by watchdog.
- `healthy`: no action.
- `unknown`: log only unless a deterministic rule says otherwise.

The watchdog log should record deterministic decisions with `source:
heuristic`. Ambiguous cases should remain `unknown`/observe-only instead of
being escalated to an LLM.

---

## 7. User-Visible Notifications

### 7.1 Resolve The Right Chat

For an event-specific issue, notify the originating conversation:

1. decoded `event.meta.delivery_channel`, if set;
2. otherwise `router.channel_name(event)`;
3. channel-specific destination from meta:
   - Telegram: `meta.chat_id`, plus `message_thread_id` when present;
   - Slack: channel/thread identifiers already used by delivery;
   - Discord: channel/thread identifiers already used by delivery;
   - voice: paired channel text fallback.

If the event came from a group but the issue requires an operator secret
(`auth_expired`), notify the operator DM from instance config instead of the
group, and send a generic group-safe message to the originating group:

```text
I hit an authentication issue with the current brain. I notified the operator
privately and will retry this when the session is restored.
```

If the event is system-originated (`cron`, `jc-events`) and no user chat exists,
notify the configured operator chat.

### 7.2 Long-Running Message

After `long_running_notice_seconds` (default 180s), the watchdog records a
`long_running` decision for visibility only. It must not send a generic chat
message such as "taking longer than usual" and must not ask an LLM to invent a
progress note.

Specific human progress messages belong in the active brain/runtime flow, where
the system has real task context. If that flow cannot prove what work is being
done, it should say nothing rather than send static filler.

### 7.3 Brain-Unhealthy Message

When a brain failure is tied to a specific active event:

```text
I could not get a clean answer for "<request preview>" because the current brain
hit a runtime/auth issue. I notified the operator.
```

Messages must be sent directly through `deliver_response()` or channel clients,
not through a brain.

---

## 8. Brain Health Cooldown

Watchdog may mark a brain temporarily unavailable when active runtime/auth
evidence is clear. This state is advisory for watchdog decisions and operator
visibility; it is not a replay or switching mechanism.

```yaml
watchdog:
  intelligent: true
  brain_health_cooldown_seconds: 900
```

On `auth_expired` or repeated `brain_unhealthy` decisions, mark the current
brain unavailable:

```json
{"state":"unavailable","reason":"auth_expired","until":"..."}
```

The cooldown prevents repeated watchdog notices for the same broken brain.
`jc watchdog reset`, or cooldown expiry clears the mark.

---

## 9. Auth Expiry Integration

Existing recovery code already knows how to classify `session_expired` and ask
the operator for re-auth. The intelligent watchdog should not duplicate token
redemption.

Instead:

1. watchdog detects evidence of auth expiry in logs for active running events;
2. evaluator classifies `auth_expired`;
3. watchdog marks the brain unavailable and sends user-visible status;
4. watchdog notifies the operator privately.

Codex-specific expiry must be supported too. If the failed brain is `codex` or
`codex_api`, the operator action should point at `jc codex-auth refresh` or the
configured Codex auth recovery path, not `claude /login`.

v1 behavior:

- Watchdog never switches or retries failed events.
- Watchdog never creates `auth_pending` rows for replay.
- Group chats receive only a generic status message; token/login instructions
  go to the configured operator DM.

---

## 10. Observability

Add structured log kinds:

- `watchdog_intelligence_snapshot`
- `watchdog_intelligence_decision`
- `watchdog_long_running_observed`
- `watchdog_brain_unhealthy`
- `watchdog_brain_cooldown`
- `watchdog_notify_failed`

Add CLI visibility:

```bash
jc watchdog status --json
jc watchdog intelligence
jc watchdog reset-brain claude
```

`jc watchdog intelligence` should show:

- running events over threshold;
- latest evaluator decisions;
- brain health marks/cooldowns;
- notification dedupe state.

---

## 11. Safety Rules

- Do not send secrets, login URLs, or token instructions to a group chat.
- Do not ask the stuck brain to explain its own stuck state.
- Do not send watchdog long-running progress messages. Static progress filler is
  worse than silence.
- Do not switch, retry, or replay user message events from watchdog.
- Do not scan terminal failed rows as a source of unanswered-message recovery.
- Do not mark a brain globally unavailable from one soft timeout. Require
  either high-confidence auth/session evidence or repeated failures.
- Do not hide the original failure. Log the brain, evaluator output, and event
  id.

---

## 12. Implementation Plan

### Phase 1 — Snapshot and Status

- Add `lib/watchdog/intelligence/snapshot.py`.
- Read queue running rows and recent gateway logs.
- Add `jc watchdog intelligence --json`.
- No notifications, no replay.

### Phase 2 — Long-Running Notices

- Add state dedupe.
- Record running events older than 180s.
- Do not send direct channel notification for long-running events.
- Tests verify long-running events are observed without user-visible actions.

### Phase 3 — Brain Health Evaluation

- Add deterministic evaluator rules for clear runtime/auth evidence.
- Classify recent failure/log snapshots.
- Mark brain `suspect` / `unavailable` with cooldowns.
- Notify the operator for active auth/runtime issues without creating replay
  state.

### Phase 4 — Failed-Event Replay Removal

- Remove failed-row scanning from watchdog.
- Remove fallback config and candidate selection from watchdog.
- Ensure watchdog never calls `queue.retry_now()` or creates `auth_pending`.

### Phase 5 — V2 Supervisor Integration

- Call intelligent tick from `lib/watchdog/supervisor.py` when gateway child is
  healthy.
- Keep bash watchdog compatibility via a helper command until v2 install is the
  only path.

---

## 13. Test Plan

### Unit

- Snapshot reads running events older than threshold.
- Snapshot does not read terminal failed events for recovery.
- Snapshot parses bounded JSON log windows and ignores malformed lines.
- Evaluator classifies auth/runtime failures deterministically.
- Long-running events produce no user-visible watchdog action.
- Failed auth events remain failed and are not retried/switched by watchdog.
- Brain cooldown expiry clears stale unavailable marks.

### Integration

- Running Telegram event older than 180s records a decision but sends no
  Telegram progress message.
- Group event with auth expiry sends generic group notice plus private operator
  DM.
- Claude auth evidence on an active running event marks `claude` unavailable and
  sends notification without replaying the event.
- Codex auth-expired failure points operator at Codex auth recovery, not Claude
  login.
- Failed events are not scanned, switched, or replayed by watchdog.
- Long-running event that later completes does not receive watchdog notices.

### Manual Smoke On Ada

1. Configure `watchdog.intelligent: true`.
2. Inject a fake running event older than 180s; run watchdog tick; confirm a
   recorded `long_running` decision and no user-visible progress message.
3. Force a Claude auth log on an active running event; run watchdog tick;
   confirm private operator alert and no event retry/switch.
4. Confirm normal healthy gateway tick produces no user-visible messages.

---

## 14. Open Questions

Resolved for v1:

1. The first long-running evaluation uses the same 180s default for all chats,
   but watchdog is observe-only for long-running work. Operators can tune this
   with `watchdog.long_running_notice_seconds`.
2. Watchdog does not switch, retry, or replay terminal failed events. Gateway
   recovery owns adapter-failure replay.
3. v1 does not kill a still-running adapter process. It may notify on clear
   runtime/auth failures and marks brain health cooldowns only.

Open for v2:

1. Should group chats get a longer default notice threshold once we have usage
   data?
2. Should the gateway track adapter process ids so the watchdog can safely
   cancel a known-bad running adapter before its normal timeout?
