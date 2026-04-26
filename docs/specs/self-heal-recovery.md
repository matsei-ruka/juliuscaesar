# Self-Heal Recovery — Adapter Failure Classifier + Login Re-auth Flow

**Status:** Spec (needs review)
**Author:** Rachel
**Date:** 2026-04-26

## Goal

When an adapter exits non-zero, the gateway dispatcher currently retries up to 4× with backoff regardless of the failure mode. Recent stability data: 5 events permanently failed (all image-related, all bad input retried 4× wastefully); zero events recovered from `claude` session expiry because nothing detects it; and resumes against deleted/missing session-ids loop quietly until they hit `max_retries`. This spec makes the dispatcher classify the failure first, then route to the correct recovery: retry, fail-fast, silently drop a dead session-id and start fresh, or trigger a human-in-the-loop re-auth flow. The login-recovery flow is the headline feature: a Telegram round-trip that lets the operator paste a token without leaving the chat.

## Architecture

### Error classifier

Hooked into `lib/gateway/runtime.py` at the point where adapter rc is checked. New module: `lib/gateway/recovery/`.

```
lib/gateway/recovery/
  __init__.py
  classifier.py        # async classify(stderr_tail, event) -> Classification
  prompt.md            # triage prompt (see below)
  handlers/
    __init__.py
    base.py            # RecoveryHandler ABC
    transient.py       # delegates to existing retry path
    bad_input.py       # mark failed, no retry
    session_expired.py # triggers login-recovery flow
    session_missing.py # clears sticky session-id, redispatches once fresh
    unknown.py         # 1 retry then fail; full stderr to log
  state.py             # auth_pending CRUD on queue.db
```

Classification calls the same OpenRouter triage backend already configured in `lib/gateway/triage/openrouter.py` (cheap model, low latency, low cost). It returns:

```python
@dataclass(frozen=True)
class Classification:
    kind: Literal["transient", "session_expired", "session_missing", "bad_input", "unknown"]
    confidence: float
    extracted: dict  # e.g. {"login_url": "https://..."} for session_expired
                    #      {"session_id": "<uuid>"} for session_missing
    raw: str
```

Confidence < 0.6 → fall through to `unknown`. Stderr tail is capped at 80 lines / 8KB before sending to the classifier.

### Recovery handler interface

```python
class RecoveryHandler(Protocol):
    async def handle(
        self,
        event: queue.Event,
        classification: Classification,
        ctx: RecoveryContext,
    ) -> RecoveryDecision: ...
```

`RecoveryDecision` is one of: `Retry(delay)`, `Fail(reason)`, `Defer(reason)` (event remains in flight; handler arranges its own re-enqueue).

A registry maps `kind` → handler. New error types (e.g., `rate_limited`, `quota_exhausted`) plug in by adding a class to the registry; no dispatcher changes needed.

### auth_pending state table

New table in `state/gateway/queue.db` (schema bump):

```sql
CREATE TABLE auth_pending (
    id            INTEGER PRIMARY KEY,
    event_id      INTEGER NOT NULL REFERENCES events(id),
    operator_chat TEXT NOT NULL,         -- chat_id to listen on for token
    login_url     TEXT NOT NULL,
    requested_at  TEXT NOT NULL,         -- ISO8601 UTC
    expires_at    TEXT NOT NULL,         -- requested_at + 10 min
    state         TEXT NOT NULL          -- waiting | redeeming | done | expired | failed
);
CREATE UNIQUE INDEX auth_pending_one_active
    ON auth_pending(operator_chat) WHERE state IN ('waiting', 'redeeming');
```

The unique partial index enforces "at most one outstanding auth request per operator chat" — prevents the operator from getting two competing prompts if two events fail back-to-back.

### Dispatch flow

```
adapter exits rc != 0
  → capture stderr tail (80 lines)
  → recovery.classify(stderr, event)
  → handler = registry[classification.kind]
  → decision = handler.handle(event, classification, ctx)
  → match decision:
      Retry(delay) → re-enqueue with available_at = now + delay
      Fail(reason) → mark event failed; record reason; do NOT retry
      Defer(reason) → leave event in flight; handler owns re-enqueue
```

The classifier itself is a network call; if it fails or times out (5s cap), default to the existing retry behavior (preserves current semantics on classifier outage).

## Triage prompt

Stored at `lib/gateway/recovery/prompt.md`:

```
You are an error classifier for a JuliusCaesar gateway dispatcher.

You receive: the original event content (truncated) and the stderr tail of an
adapter process that exited non-zero. You return exactly one JSON object on a
single line.

Schema: {"kind":"<kind>","confidence":<0..1>,"extracted":{...}}

Kinds:
- transient        → network error, 5xx from upstream, timeout, "connection reset",
                     "EAI_AGAIN", "context deadline exceeded". Safe to retry.
- session_expired  → claude/openrouter auth credential is invalid or expired.
                     Look for: "please run /login", "Authentication failed",
                     "401 Unauthorized" from the LLM provider, "session expired",
                     a URL of the form https://claude.ai/... or
                     https://console.anthropic.com/... in the stderr.
                     If a login URL is present, return it as extracted.login_url.
- session_missing  → --resume <id> rejected because the session does not exist
                     (file deleted, account switched, fresh install). Look for:
                     "No conversation found with session ID <uuid>",
                     "Session <uuid> not found", "Failed to resume",
                     "unknown session". Auth itself is fine — only the resume
                     target is gone. Extract the uuid into extracted.session_id
                     when present. Distinguish from session_expired: if both
                     auth-failure markers AND missing-session markers appear,
                     pick session_expired (re-auth covers both).
- bad_input        → malformed event, oversized payload, unsupported file type,
                     image too large, MIME mismatch, "invalid base64", schema
                     violation. Retrying will not help; the input itself is wrong.
- unknown          → none of the above clearly applies, or the stderr is empty.

Confidence is your own self-rating (0..1). The dispatcher treats anything < 0.6
as "unknown" regardless of the kind you return.

Examples:

stderr: "ECONNRESET reading from api.openrouter.ai"
→ {"kind":"transient","confidence":0.95,"extracted":{}}

stderr: "Your session has expired. Please run: claude /login\nVisit https://claude.ai/cli/auth?token=abc to re-authenticate."
→ {"kind":"session_expired","confidence":0.98,"extracted":{"login_url":"https://claude.ai/cli/auth?token=abc"}}

stderr: "Image exceeds maximum size of 5MB (got 12MB)"
→ {"kind":"bad_input","confidence":0.97,"extracted":{}}

stderr: "Error: No conversation found with session ID 7d5ec0b5-47a6-4ff3-ae5f-2a6a6657cf46"
→ {"kind":"session_missing","confidence":0.97,"extracted":{"session_id":"7d5ec0b5-47a6-4ff3-ae5f-2a6a6657cf46"}}

Now classify:
EVENT: {event_content}
STDERR: {stderr_tail}
```

Same parser as `lib/gateway/triage/base.py:parse_triage_json` (one-line JSON regex).

## session_missing recovery (no operator)

Silent, automatic, no Telegram round-trip. Triggered when the classifier returns `session_missing` (or as a fallback when `--resume <uuid>` returns rc≠0 with stderr matching the regex `(no conversation found|session .*(not found|unknown))`, even on classifier outage):

1. Extract `session_id` from `extracted.session_id`; fall back to a regex over the stderr tail if absent.
2. `sessions.clear(conversation_id, brain, session_id)` — remove the sticky-brain mapping in `state/gateway/sessions.db` (or wherever `lib/gateway/sessions.py` persists it) so the dispatcher no longer passes `--resume <dead_uuid>`.
3. Re-enqueue the same event with `available_at = now` and a meta flag `meta.session_missing_redispatch = True`. The next dispatch runs `claude -p` without `--resume`, claude generates a new session-id, the gateway records it as the new sticky for `(conversation_id, brain)`.
4. If the redispatched event ALSO returns `session_missing`: log error, fail the event with reason `session_missing_recovery_failed`, alert operator (one DM, throttled). Means the sticky-clear didn't take, or the brain itself is broken — needs human eyes.
5. The DM-the-operator-after-silent-recovery question is in Open Questions; default is silent on first occurrence per `(conversation_id, day)`.

This handler is the simplest one — no state table, no operator interaction, no token. Implementation cost is low and it eliminates a known silent failure mode (a stale `SESSION_ID` in `ops/watchdog.conf` after a `~/.claude/projects/` cleanup currently produces N retries × M events of pure noise).

## Login-recovery state machine

States and transitions for one auth_pending row:

```
            +-----------+
            | (no row)  |
            +-----+-----+
                  | session_expired classified, login_url extracted
                  | → DM operator, INSERT row state=waiting
                  v
            +-----------+   operator sends non-token message
            |  waiting  |---+   → row stays; message routes normally
            +-----+-----+   |
                  |         |   timeout (>10 min)
                  |         |   → state=expired, DM "auth request timed out"
                  | operator sends token-shaped message
                  v
            +-----------+
            | redeeming |
            +-----+-----+
                  | claude /login <token> rc=0
                  | → state=done, re-enqueue event_id, DM "re-auth ok, replaying"
                  v
            +-----------+
            |   done    |
            +-----------+

       on claude /login rc!=0
                  | → state=failed, DM "re-auth failed: <stderr summary>",
                  |   keep row state=failed for one retry (operator can paste again)
                  v
            +-----------+
            |  failed   |---+ operator sends another token-shaped message
            +-----------+   | → state=redeeming, retry
                            |
                            v (rc=0) → done   (rc!=0 again) → failed (final)
```

Operator message interception (a new pre-triage hook in `runtime.py`):

```python
def maybe_handle_auth_token(event: queue.Event) -> bool:
    pending = state.get_active_pending(event.user_id_or_chat)
    if not pending: return False
    if not looks_like_token(event.content): return False  # let normal triage run
    redeem(pending, event.content, event_to_replay=pending.event_id)
    return True  # event consumed, do not triage
```

`looks_like_token`: regex `^[A-Za-z0-9._\-]{20,}$` with the additional rule "no whitespace, exactly one token on the line." False positives are mitigated by the "active pending" gate — without an outstanding request, no message is ever interpreted as a token.

`claude /login <token>` runs headless via `subprocess.run(["claude", "/login", token], capture_output=True, timeout=30)`. If the binary tries to open a browser (which it normally does in interactive mode), the headless `--no-browser` flag is passed; if that flag does not exist on the installed Claude version, the spec needs a small wrapper that pipes the token via stdin instead. **Open question — see below.**

## Edge cases

- **Operator sends multiple tokens.** The unique partial index means only one auth_pending row is `waiting|redeeming` at a time. The first token transitions to `redeeming`; subsequent token-shaped messages while `redeeming` are queued behind the redemption (≤30s) and processed in arrival order. If the redemption is `done` by the time the second token arrives, it does not match an active pending and routes normally.
- **Operator never replies.** The 10-minute `expires_at` is checked every supervisor tick (or every poll iteration). Expired rows: state→`expired`, DM "auth request timed out — original event will not be replayed automatically. Send the failed message again to retry," failed event stays failed.
- **Multiple events pile up while waiting.** Each new failure that classifies as `session_expired` while a row is `waiting`: do **not** create a second row (unique index would reject). Append the new event_id to a `pending_events` JSON column on the existing row. On successful redemption, replay all queued event_ids in order.
- **Token format false positives.** Plain English phrases like "antidisestablishmentarianism" pass the length+charset check. Mitigation: the `active pending` gate plus the "single line, no whitespace" rule. A genuine collision (operator pastes a 20+ char no-space alphanumeric string that is not a token, while a pending exists) → `claude /login` rc!=0 → operator sees "re-auth failed" and pastes the real token. Acceptable.
- **`claude /login` opens a browser interactively.** In headless contexts (no DISPLAY, no `--no-browser` support) it can hang. Mitigation: the subprocess call has a 30s timeout; on timeout we kill the process tree, mark `failed`, and DM the operator with "re-auth tool blocked on browser; run `claude /login` in your screen session and tell me when done." We then wait for the operator to send the literal string `done` (or `/auth-done`) which clears the pending and replays.
- **`session_missing` while sticky-clear is racing.** Two events for the same `(conversation_id, brain)` fail concurrently with `session_missing`. The first handler clears the sticky and redispatches; the second handler reads an empty sticky, treats its own redispatch as already covered, and fails fast with `session_missing_racing`. Re-enqueue the second event behind the first. Implement via a per-conversation lock in `sessions.py`.
- **`session_missing` after operator-driven re-auth.** Possible if the operator authed under a different account whose `~/.claude/projects/` has none of the resumed session-ids. The dispatcher's sticky-clear + retry path covers it; the operator never sees a separate prompt.
- **Stale `SESSION_ID` in `ops/watchdog.conf`** (legacy-claude only). Not the gateway's problem — the legacy-claude child in watchdog v2 is what consumes that file. Watchdog spec calls this out; gateway-mode instances do not read `SESSION_ID` from disk.
- **Operator's chat is a group, not a DM.** Auth flow only fires for `chat.type == 'private'` chats with the configured operator user_id. Group chats with a session expiry are silenced (the failed event is marked failed with reason `auth_required_in_group`); the operator must trigger re-auth from their DM.
- **The classifier itself fails.** If OpenRouter returns 5xx or times out, treat as `transient`; the existing retry path runs. We never block the dispatcher waiting on the classifier.
- **The classifier hallucinates a login_url.** Validate `extracted.login_url`: must be `https://`, must be on a known host (`claude.ai`, `console.anthropic.com`, `openrouter.ai`). Otherwise drop the URL, fall through to a generic prompt: "Claude session expired. Run `claude /login` in your screen session and reply with the token here."

## Security

- **Tokens transit Telegram in plaintext.** Telegram client/server is TLS, server storage is not E2E. For this user (single operator, personal device) this is acceptable; calling it out so the trade-off is explicit.
- **Tokens are never logged.** Audit log entries for the auth flow record `event_id`, `state`, `classification.kind`, and a token *fingerprint* (first 4 chars + sha256[:8]) — never the full token. The `state.py` module has a `_redact(token)` helper used at every log call site.
- **Tokens are never persisted.** The `redeeming` state holds the token only in the Python frame for the duration of the subprocess call; it is not written to `auth_pending`, not written to event response bodies, not echoed to the DM.
- **Subprocess argv exposes the token to anyone with `/proc` access on the host.** Mitigation: pipe via stdin (`claude /login` with no token arg → reads from stdin). Confirms Open question 2.
- **Operator identity check.** The interception only runs when `event.user_id == config.operator_user_id` AND `event.chat_type == 'private'`. A token-shaped message from anyone else is routed normally.

## Open questions

- **Does `claude /login` accept a token via stdin?** If yes, prefer it — keeps the secret out of `/proc/<pid>/cmdline`. If no, file an upstream feature request and live with argv exposure (single-tenant host, low risk).
- **Does the installed Claude have `--no-browser` or equivalent?** If not, the "claude opens a browser and hangs" branch is the unhappy default and the operator-types-`done` workaround is the actual primary path. Worth confirming before estimating implementation.
- **Should the classifier also see the *previous* attempt's classification?** A flapping `session_expired → transient → session_expired` cycle suggests something stranger. Pass `event.retry_count` and the prior classification (if any) as extra context — small change, possibly meaningful signal.
- **Should `bad_input` notify the operator?** Today's silent fail-fast keeps the chat clean; but if every image the operator sends gets quietly dropped, that is also bad. Proposal: DM the operator on the *first* `bad_input` per conversation per hour, then silence for the rest of that hour.
- **Per-brain re-auth.** If we add an OpenRouter-specific session expiry handler later, the recovery handler interface needs a `brain` dimension in the registry. Punt until OpenRouter actually expires us.
- **Idempotency on replay.** When we re-enqueue the failed event after re-auth, does the original Telegram message get a second response sent? Need to confirm the dispatcher's de-dup keys cover the re-enqueue path.
- **Notify on silent `session_missing` recovery?** Default proposed: silent on first occurrence per `(conversation_id, day)`; DM if it repeats within an hour (signals a real bug, not a one-off cleanup). Worth one DM the very first time per conversation so the operator knows their conversation memory in that brain has reset?
- **Where does `session_missing` get classified?** Stderr signature is stable enough that a regex prefilter could short-circuit the LLM call (saves ~1s of classifier latency on a known failure mode). Proposal: cheap regex prefilter for the four most common stderr signatures (`session_missing`, `session_expired`, OOM `bad_input`, ECONNRESET `transient`); LLM only on miss. Kills classifier cost for the most common failures.
