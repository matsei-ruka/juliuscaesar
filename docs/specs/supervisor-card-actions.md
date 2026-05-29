# Spec: Supervisor card actions (Stop + Background)

**Status:** Draft
**Date:** 2026-05-29
**Branch:** `feature/supervisor-card-actions`
**Owner:** tbd
**Trigger incident:** 2026-05-29 — Noah hung 4h on a tool loop, locking conversation slot. Operator's only escape was SSH + manual kill. Backgrounding doesn't exist; operator cannot send a new message to the agent while a long task runs.

---

## 1. Goal

Give the operator mid-flight control over running brain sessions via inline buttons on supervisor cards in Telegram. Two actions:

- **Stop** — kill the running brain child process, release its parallel slot, mark the session terminated. Operator regains the slot immediately and can send a new message.
- **Background** — demote the running session so a fresh main session takes over the chat. The old session keeps running to completion; its final output is intercepted and delivered as a "background task complete" notification card. Operator can talk to a new main session while the heavy task finishes.

### Feature goal (success in one line)

> An operator who sees a supervisor card for a long-running task can press one button to either kill the task or move it to background, and continue working in the same Telegram chat without waiting for the task to finish or SSHing into the box.

### Expected outcomes

- **Stop:** SIGTERM lands on the brain child within 2s of button press; slot released; card edited to "Stopped ✋ · <duration>"; next inbound message starts a fresh session.
- **Background:** Old session continues running unchanged; gateway routes new inbound messages for the same `chat_id` to a fresh session; when the old session produces its final reply, gateway intercepts it and posts a worker-completion card (`🔄 Background done: <output>`); card on the original supervisor message edited to "Backgrounded 🔄 · running".
- **Noah 4h-hang scenario:** never happens again. Operator presses Stop within 30s of noticing the card stop progressing.

---

## 2. Current state

- Supervisor cards rendered by `lib/supervisor/cards.py` and delivered by `lib/supervisor/delivery.py`. Cards are plain Telegram messages, no `reply_markup`.
- Gateway spawns a brain child per event in `lib/gateway/runtime.py`. Child PID is known to the runtime; not surfaced anywhere external.
- Parallel slots per `conversation_id` configured by `gateway.parallel_slots` (default 1, max 2 today). Slot held for the lifetime of the brain child.
- `adapter_timeout_seconds` (default 300s, Noah was 28800s before fix) SIGTERMs the child after timeout — passive only, never user-triggered.
- Telegram channel already handles `callback_query` updates for chat-auth approvals (`lib/gateway/channels/telegram.py:_handle_callback_query` at line 555). Inline keyboards already supported (line 436).
- No concept of "worker session" tagged on an existing session today. `jc workers spawn` creates fresh sessions from scratch.

---

## 3. Non-goals

- **Resume of stopped sessions.** Stop is terminal. Operator can re-prompt manually if needed.
- **Mid-flight prompt injection into a running brain.** Architecturally impossible — running brain process does not poll for new instructions. Background is a routing trick, not a brain trick.
- **Per-tool kill.** Stop terminates the whole session, not individual tool calls.
- **Non-Telegram channels** (Slack, Discord, voice, email). Phase 4+.
- **Cross-conversation Stop/Background** (kill a session in chat A from chat B). Out of scope.
- **Persistence across gateway restart.** If gateway restarts, in-flight background sessions are lost; the next watchdog tick handles cleanup. v2 may add durable state.

---

## 4. Architecture

### 4.1 Card payload

Every supervisor card now carries a hidden handle: `(session_id, conversation_id, chat_id, supervisor_msg_id)`. Gateway needs all four to route a button press back to the right session and edit the right card.

`callback_data` is bounded to 64 bytes by Telegram. Encode as `act:<verb>:<short_token>` where `<short_token>` is the first 12 chars of `session_id` (UUIDv4 collision-safe within an instance). Gateway maintains an in-memory `short_token → session_id` map populated when cards are rendered.

### 4.2 Inline keyboard

Two-button row appended to every supervisor card while the session is `running`:

```
[ ✋ Stop ]  [ 🔄 Background ]
```

After Stop: keyboard removed; card text edited to append `\n\n✋ Stopped at <hh:mm:ss UTC> · <duration>`.
After Background: keyboard replaced with single disabled button `[ 🔄 Backgrounded · running ]`; card text appended with `\n\n🔄 Backgrounded at <hh:mm:ss UTC>`.
After background completion: keyboard removed; card text replaced with the captured final reply prefixed by `🔄 Background done · <duration>:\n\n`.

### 4.3 New module: `lib/gateway/actions.py`

Public API:

```python
def stop_session(session_id: str) -> StopResult:
    """SIGTERM the brain child, release the slot, mark session terminated."""

def background_session(session_id: str) -> BackgroundResult:
    """Tag session as worker; future chat_id inbound routes to a fresh session."""
```

Both handlers are idempotent (double-tap = no-op with explicit "already stopped" / "already backgrounded" answer to the callback_query).

### 4.4 Session role layer

Extend session state in `lib/gateway/runtime.py`:

```
SessionRole = Literal["primary", "backgrounded"]
session_state[session_id] = {
    "role": "primary",
    "chat_id": "...",
    "started_at": ts,
    "child_pid": ...,
    "slot_id": ...,
}
```

Routing rule (new): when an inbound event arrives for `chat_id=X`, find the `primary` session for `X`; if none, spawn fresh. Backgrounded sessions are invisible to the inbound router.

### 4.5 Output interception

When a backgrounded session produces a reply (via existing reply pipeline), gateway intercepts:

1. **Final brain reply** (the `assistant` turn at end of execution) → wrapped as `🔄 Background done: <text>` and sent as a new Telegram message to the same `chat_id`. Also edits the original supervisor card to the completion state described in §4.2.
2. **Mid-task tool calls that send Telegram messages** (e.g. `send_telegram` skill, `telegram_outbound` invocations from inside the session) → suppressed. A buffer captures them; the buffered messages get prepended to the final-reply card (or omitted if buffer is empty and final reply is empty).

Suppression must be opt-out per-instance (`gateway.actions.suppress_background_tool_messages: true` by default).

### 4.6 Telegram callback handler

Extend `_handle_callback_query` in `lib/gateway/channels/telegram.py`:

```python
data = cq.get("data", "")
if data.startswith("act:stop:"):
    self._handle_stop_action(cq, data[len("act:stop:"):])
elif data.startswith("act:bg:"):
    self._handle_background_action(cq, data[len("act:bg:"):])
elif data.startswith("auth:"):
    # existing chat-auth flow, unchanged
    ...
```

Each handler:
1. Resolve `short_token → session_id` from gateway action registry.
2. Verify the callback_query `from.id` matches an authorized chat_id for this instance.
3. Call `actions.stop_session` or `actions.background_session`.
4. Answer the callback_query with status text ("Stopping…", "Backgrounded").
5. Edit the supervisor card via `editMessageText` + `editMessageReplyMarkup`.

### 4.7 Authorization

Only chat_ids in `channels.telegram.chat_ids` may press these buttons. Unauthorized presses receive `answerCallbackQuery` with "Not authorized" and no action runs.

---

## 5. Phases

### Phase 1 — Foundation + Stop button (target: 1 week)

**Goal:** every supervisor card carries a working Stop button on Telegram. Pressing it kills the brain child, frees the slot, edits the card.

Deliverables:
1. `lib/gateway/actions.py` with `stop_session()` implemented.
2. `lib/supervisor/cards.py` extended to emit `reply_markup` when feature flag on.
3. `lib/supervisor/delivery.py` registers `(session_id, short_token, supervisor_msg_id)` with the action registry on send.
4. `lib/gateway/channels/telegram.py` handles `act:stop:<token>` callbacks.
5. `lib/gateway/config.py` adds `gateway.actions.enabled: bool` (default `false`) and `gateway.actions.stop_grace_seconds: int` (default 5 — SIGTERM then SIGKILL after grace).
6. New tests: `tests/test_supervisor_actions_stop.py` covering happy path, double-tap idempotency, unauthorized press, missing session.
7. Manual verification on Noah (codex brain) + Rachel (claude brain) before declaring done.

Acceptance: long-running session, press Stop → brain child gone within 5s, slot freed, card edited, next inbound message lands cleanly.

### Phase 2 — Background button (target: 2 weeks)

**Goal:** Background button works for both claude and codex brains. Operator can press Background and immediately send a new message that gets a fresh session reply, while the old session drains.

Deliverables:
1. Session role layer (`lib/gateway/runtime.py` extensions described in §4.4).
2. `background_session()` in `actions.py`.
3. Inbound router (new function in `runtime.py`) consults role layer when picking a session for incoming events.
4. Output interception:
   - Hook on the brain adapter's reply-emit path. For codex/pi, intercept stdout-final-message. For claude, intercept `assistant` turn from session JSONL.
   - Tool-side suppression: wrap `send_telegram`-equivalent skill invocations from a backgrounded session.
5. Completion card render: new card style ("Background done") replacing the original card.
6. `lib/gateway/config.py` adds `gateway.actions.suppress_background_tool_messages: bool` (default `true`) and `gateway.actions.max_background_per_chat: int` (default 3 — refuse to background a 4th concurrent session in same chat).
7. Tests: `tests/test_supervisor_actions_background.py` covering: backgrounded session completes successfully; new session responds in parallel; suppression of mid-task telegram tool calls; max-background limit enforced.
8. Manual verification on Noah (codex) + Rachel (claude): trigger long task, background, send second message, both complete cleanly.

Acceptance: backgrounded session never blocks new chat traffic; its completion arrives as a clearly-marked notification, not as a normal chat reply.

### Phase 3 — Polish (target: 3-4 days)

Deliverables:
1. Callback debouncing: 2s lockout per `session_id` to prevent double-tap races.
2. Card state machine documented: `Running → {Stopped, Backgrounded → Completed}`.
3. Audit log: every action appended to `state/actions.jsonl` with `{ts, session_id, chat_id, verb, actor_chat_id, result}`.
4. `jc doctor` check: surface backgrounded sessions still running past `adapter_timeout_seconds × 2` (likely leaked).
5. `jc actions list` CLI: list active backgrounded sessions for an instance.
6. Rollout: flip default `actions.enabled: true` for all fleet instances after Phase 2 stable for 7 days.

Acceptance: zero leaked backgrounded sessions in 7-day fleet observation window; audit log complete.

---

## 6. Design decisions (resolved 2026-05-29)

1. **Stop signal:** SIGTERM + `stop_grace_seconds=5` then SIGKILL. ✅ graceful.
2. **Background concurrency cap:** `max_background_per_chat=3`. ✅ proposed.
3. **Background completion delivery:** fresh message + edit original card. ✅ proposed.
4. **Authorization tier:** any chat_id that passed `chat_auth` (not just operator-only). ✅ open.
5. **Codex final-reply detection:** stdout EOF + exit code. ✅ proposed.

---

## 7. File touchpoints

New:
- `lib/gateway/actions.py` (stop + background handlers, action registry)
- `tests/test_supervisor_actions_stop.py`
- `tests/test_supervisor_actions_background.py`

Modified:
- `lib/supervisor/cards.py` (append `reply_markup`)
- `lib/supervisor/delivery.py` (register card with action registry on send)
- `lib/supervisor/models.py` (carry session handle in card model)
- `lib/gateway/channels/telegram.py` (`_handle_callback_query` extension; `editMessageText`/`editMessageReplyMarkup` helpers)
- `lib/gateway/runtime.py` (session role layer, child_pid registry, inbound router consult)
- `lib/gateway/config.py` (`actions.*` config block, validation)
- `docs/specs/supervisor-card-actions.md` (this file)

Touched but not necessarily modified:
- `lib/supervisor/runner.py` (may need to pass session handle through render path)
- `lib/heartbeat/adapters/codex.sh` / `claude.sh` (intercept-friendly reply emission — investigate during Phase 2)

---

## 8. Backward compatibility

- All behavior gated on `gateway.actions.enabled` (default `false`). Existing fleet instances see no change until the flag flips.
- Cards without buttons render exactly as today when flag is off.
- Existing chat-auth callback flow untouched (different callback_data prefix).
- `parallel_slots`, `adapter_timeout_seconds`, watchdog behavior all unchanged.
- Rollout plan: Phase 1 enabled on Rachel only → 48h soak → enabled on Noah + Victoria → 7-day soak → fleet-wide default `true` in Phase 3.

---

## 9. Risks & mitigations

| Risk | Mitigation |
|------|-----------|
| SIGTERM leaves dirty filesystem state (half-written file, partial DB write) | Same risk as `adapter_timeout_seconds` today — acceptable. Documented behavior. |
| Backgrounded session leaks (gateway restart loses tracking) | Phase 3 `jc doctor` check + `jc actions list`; long-term durable state in v2. |
| Telegram callback_query rate limit (30/sec per bot) | Already handled by single-threaded callback poller; debouncing in Phase 3 further protects. |
| `short_token` collision (12-char UUID prefix) within an instance | UUIDv4 → birthday bound at ~2^48; in-memory map cleaned on session end. Collision impossible at fleet scale. |
| Backgrounded codex session never emits stdout EOF (hung) | Existing `adapter_timeout_seconds` still applies — child gets SIGTERM, completion card shows "Background timed out". |
| Operator presses Stop on Rachel's own self-session (gateway IS rachel_zane) | Block in handler: refuse to stop sessions where `session_id == os.environ['JC_SESSION_ID']`. Matches existing "never restart own gateway" rule. |
