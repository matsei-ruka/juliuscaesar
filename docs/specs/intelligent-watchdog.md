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
2. ask the configured triage model to classify the situation;
3. notify the right chat with a user-visible status;
4. switch the pending request to another available brain when that is safer
   than waiting or retrying the failed brain;
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
- The originating chat is notified while a brain is still running.
- Pending requests can be moved away from a brain that is probably unhealthy.
- The configured triage model participates in watchdog health decisions.

---

## 3. Non-Goals

- Not replacing gateway retry/recovery. Existing adapter failure recovery stays
  in the gateway and remains the first responder after `invoke_brain()` returns
  or raises.
- Not killing long-running brain subprocesses at the three-minute mark. The
  first milestone is user-visible progress, not cancellation.
- Not letting the triage model choose arbitrary brains. It may recommend a
  category of action, but fallback candidates come from operator config and the
  brain capability matrix.
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
           classify with triage model when heuristic signal is present
           notify/switch/defer
```

This phase should live in Python, not in `watchdog.sh`. Preferred placement:

```text
lib/watchdog/intelligence/
  __init__.py
  snapshot.py       # queue/log/config snapshot
  evaluator.py      # triage-backed classifier
  decisions.py      # dataclasses for decisions
  actions.py        # notify, switch, mark state
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
      "long_running_first_notice_at": "2026-05-12T10:05:00Z",
      "long_running_last_notice_at": "2026-05-12T10:12:00Z",
      "brain_switch_notice_at": "2026-05-12T10:07:00Z"
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
full prompts into the triage call.

### 5.1 Queue Snapshot

Read from `state/gateway/queue.db`:

- running events where `started_at` or `received_at` is older than
  `watchdog.long_running_notice_seconds`;
- recently failed events, especially failures with `error` mentioning
  `authentication`, `login`, `session`, `401`, `timeout`, or adapter names;
- retry counts for the same event;
- `source`, `conversation_id`, `user_id`, and decoded `meta` for delivery.

For long-running detection, use `started_at` when present; fall back to
`received_at`. Default threshold:

```yaml
watchdog:
  long_running_notice_seconds: 180
  long_running_notice_requires_triage: true
```

Failed recovery must also be bounded by count, event age, and conversation
recency so a quiet instance cannot resurrect week-old or superseded unanswered
messages:

```yaml
watchdog:
  failed_event_max_age_seconds: 3600
  failed_event_limit: 50
```

Use `started_at` when present and `received_at` otherwise. Only terminal
`failed` events are watchdog recovery candidates; `queued` events still belong
to the gateway retry loop. Skip any event already owned by gateway recovery
(`error` starting with `recovery:`, `meta.recovery_deferred`, or recovery
source-message ids). Also skip a failed event if a newer event exists in the
same source/conversation, because the chat has moved on. If
`failed_event_max_age_seconds <= 0`, the age guard is disabled for explicit
operator override only.

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
- enabled brains and configured fallback brains;
- `triage_routing`, `default_fallback_brain`,
  `triage_unsafe_fallback_brain`;
- brain capability matrix result for the event (`supports_images`, etc.);
- delivery channel availability.

---

## 6. Triage-Backed Evaluator

Use the configured triage provider/model as the evaluator engine. This should
share transport/config with `lib/gateway/triage` where practical, but it must
use a separate prompt and schema because the watchdog is not classifying user
intent; it is classifying runtime health.

New prompt:

```text
lib/watchdog/intelligence/prompt.md
```

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
  Brain switch should be considered.
- `long_running`: a running event exceeded the notice threshold but there is no
  clear failure. Send a progress message; do not switch yet by default.
- `transient_slow`: network/provider slowness or one-off timeout. User-visible
  notification optional unless the event has already crossed threshold.
- `healthy`: no action.
- `unknown`: log only unless a deterministic rule says otherwise.

Heuristics may short-circuit obvious cases before the LLM call, but any
ambiguous case must be triage-evaluated. The watchdog log should record
whether a decision came from `heuristic` or `triage_model`.

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

After `long_running_notice_seconds` (default 180s), the watchdog asks the
triage model whether a human would send a progress note. The default
`long_running_notice_requires_triage: true` means no generic timer-based
message is sent when triage is unavailable or undecided.

If triage decides a notice is useful, it must provide a natural chat message
that references the actual request and visible work. Do not mention event ids,
queue state, brain/model names, or the old static template.

```text
Sto cercando proprio il video della vacanza estiva; ci metto ancora un attimo.
```

Do not claim tool activity that is not known. Avoid "I am spawning a worker"
unless there is a real worker id in state.

Optional second notice, disabled by default:

```yaml
watchdog:
  long_running_repeat_seconds: 600
```

When enabled, repeat no more than once every repeat interval, and only if the
event is still running.

### 7.3 Brain-Unhealthy Message

When a brain failure is tied to a specific event and an alternate brain is
available:

```text
I could not get a clean answer for "<request preview>" because the current brain
hit a runtime/auth issue. I am retrying it with <fallback brain> now.
```

When no alternate is available:

```text
I could not get a clean answer for "<request preview>" because the current brain
hit a runtime/auth issue. I notified the operator and will retry when the
session is healthy again.
```

Messages must be sent directly through `deliver_response()` or channel clients,
not through a brain.

---

## 8. Brain Switching

### 8.1 Availability

A brain is available only if:

- it is configured/enabled for this instance;
- its adapter validates locally;
- it is not marked `unavailable` in watchdog intelligence state;
- it supports the event's required capabilities;
- it is not the same brain that just failed;
- it is allowed by operator policy.

New config:

```yaml
watchdog:
  intelligent: true
  brain_switch_enabled: true
  brain_switch_cooldown_seconds: 900
  brain_fallbacks:
    claude: ["codex", "gemini", "opencode"]
    codex: ["claude", "gemini"]
    gemini: ["claude", "codex"]
    opencode: ["claude", "codex"]
```

Fallback candidates are ordered. The triage evaluator decides whether a switch
is appropriate; config decides which candidates are legal.

### 8.2 Switching A Pending Event

For a failed event:

1. patch `event.meta.brain_override` to the selected fallback brain;
2. add `event.meta.watchdog_switch`:
   ```json
   {
     "from": "claude",
     "to": "codex",
     "reason": "auth_expired",
     "at": "2026-05-12T10:07:00Z"
   }
   ```
3. retry the same event once with the fallback brain.

Queued events are not switched by watchdog; they are still controlled by the
gateway retry loop. Events already marked by gateway recovery are not switched
by watchdog.
3. call `queue.retry_now(conn, event.id)`.

For a currently running event:

- Do not run the same event concurrently in two brains by default.
- If the evaluator says `auth_expired` or `brain_unhealthy` and the current
  adapter process has already failed/returned, use the failed-event path above.
- If the process is merely long-running, notify only.
- A future implementation may support a hard cancellation path, but that needs
  process ownership tracking and is out of scope for v1.

### 8.3 Brain Cooldown

On `auth_expired` or repeated `brain_unhealthy` decisions, mark the current
brain unavailable:

```json
{"state":"unavailable","reason":"auth_expired","until":"..."}
```

The cooldown prevents immediate re-routing back to a known-bad brain. Recovery
success, `jc watchdog reset`, or cooldown expiry clears the mark.

---

## 9. Auth Expiry Integration

Existing recovery code already knows how to classify `session_expired` and ask
the operator for re-auth. The intelligent watchdog should not duplicate token
redemption.

Instead:

1. watchdog detects evidence of auth expiry in logs/failed events;
2. evaluator classifies `auth_expired`;
3. watchdog marks the brain unavailable and sends user-visible status;
4. watchdog invokes or reuses the existing recovery/session-expired path to
   notify the operator privately;
5. when recovery succeeds, queued events are retried and the brain health mark
   is cleared.

Codex-specific expiry must be supported too. If the failed brain is `codex` or
`codex_api`, the operator action should point at `jc codex-auth refresh` or the
configured Codex auth recovery path, not `claude /login`.

v1 behavior:

- When a fallback brain is selected, the event is switched and retried; the
  operator still receives an auth-specific message for the failed brain.
- When no fallback is selected, watchdog creates/reuses the existing
  `auth_pending` row so the normal recovery token-redemption flow can replay
  the event after operator action.
- Group chats receive only a generic status message; token/login instructions
  go to the configured operator DM.

---

## 10. Observability

Add structured log kinds:

- `watchdog_intelligence_snapshot`
- `watchdog_intelligence_decision`
- `watchdog_long_running_notice`
- `watchdog_brain_unhealthy`
- `watchdog_brain_switch`
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
- Do not send more than one first long-running notice per event. Persist the
  dedupe both in watchdog state and event metadata so losing the watchdog state
  file does not create chat spam.
- Do not switch an event more than once unless an operator explicitly enables
  multi-switch retries.
- Do not switch queued events or recovery-owned events; that creates competing
  retry owners.
- Do not switch to a brain that lacks required capabilities, especially image
  support.
- Do not mark a brain globally unavailable from one soft timeout. Require
  either high-confidence auth/session evidence or repeated failures.
- Do not hide the original failure. Log the old brain, new brain, evaluator
  output, and event id.

---

## 12. Implementation Plan

### Phase 1 — Snapshot and Status

- Add `lib/watchdog/intelligence/snapshot.py`.
- Read queue running/failed rows and recent gateway logs.
- Add `jc watchdog intelligence --json`.
- No notifications, no brain switching.

### Phase 2 — Long-Running Notices

- Add state dedupe.
- Add direct channel notification for running events older than 180s.
- Use evaluator for ambiguous "slow vs unhealthy" classification.
- Tests verify one first notice per event and no notice after event completion.

### Phase 3 — Brain Health Evaluation

- Add evaluator prompt/schema using configured triage provider.
- Classify recent failure/log snapshots.
- Mark brain `suspect` / `unavailable` with cooldowns.
- Integrate with existing recovery/session-expired operator flow.

### Phase 4 — Brain Switch

- Add fallback config and candidate selection.
- Patch event meta with `brain_override` and `watchdog_switch`.
- Retry terminal failed events on fallback brain.
- Notify originating chat.

### Phase 5 — V2 Supervisor Integration

- Call intelligent tick from `lib/watchdog/supervisor.py` when gateway child is
  healthy.
- Keep bash watchdog compatibility via a helper command until v2 install is the
  only path.

---

## 13. Test Plan

### Unit

- Snapshot reads running events older than threshold.
- Snapshot ignores failed events older than the recovery age window, queued
  retries, recovery-managed events, and events superseded by newer messages in
  the same conversation.
- Snapshot parses bounded JSON log windows and ignores malformed lines.
- Evaluator parses valid JSON and rejects invalid/unknown kinds safely.
- Dedupe state plus event metadata prevents duplicate long-running notices.
- Candidate selection skips unavailable and capability-incompatible brains.
- Brain switch patches `meta.brain_override` and preserves existing meta.
- Auth failure without fallback creates/reuses `auth_pending`.
- Brain cooldown expiry clears stale unavailable marks.

### Integration

- Running Telegram event older than 180s sends one Telegram progress message.
- Group event with auth expiry sends generic group notice plus private operator
  DM.
- Claude auth-expired failure marks `claude` unavailable and retries on `codex`
  when configured.
- Codex auth-expired failure points operator at Codex auth recovery, not Claude
  login.
- No fallback configured: event is not switched; user/operator still notified.
- Long-running event that later completes does not receive repeat notices.

### Manual Smoke On Ada

1. Configure `watchdog.intelligent: true` and fallback brains.
2. Inject a fake running event older than 180s; run watchdog tick; confirm one
   user-visible progress message.
3. Force a Claude auth error using a controlled adapter fixture or expired test
   session; run watchdog tick; confirm private operator alert and fallback
   routing.
4. Confirm normal healthy gateway tick produces no user-visible messages.

---

## 14. Open Questions

Resolved for v1:

1. The first long-running evaluation uses the same 180s default for all chats,
   but triage decides whether a user-visible notice is warranted. Operators can
   tune this with `watchdog.long_running_notice_seconds` and can opt out of the
   triage requirement with `watchdog.long_running_notice_requires_triage: false`.
2. Brain switching is enabled by default when a configured fallback validates.
   Operators can disable it with `watchdog.brain_switch_enabled: false`.
3. v1 does not kill a still-running adapter process. It may notify on
   long-running work, but switches only terminal failed events and leaves queued
   or recovery-managed rows to their existing owner to avoid double execution.

Open for v2:

1. Should group chats get a longer default notice threshold once we have usage
   data?
2. Should the gateway track adapter process ids so the watchdog can safely
   cancel a known-bad running adapter before its normal timeout?
