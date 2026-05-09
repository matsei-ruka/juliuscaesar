# Triage `unsafe` verdict must trigger a fallback model, not a silent drop

## Status

Draft — 2026-05-09.

## Why

Today, when the triage backend (`openrouter`/`ollama`/`codex`/`claude_channel`)
classifies an event as `unsafe`, the gateway logs `triage rejected event id=N
as unsafe` and silently drops the event (`lib/gateway/runtime.py:475-481` →
`return None, True`). The user is never told their message was binned.

Real-world incident — 2026-05-09 08:50 → 09:02 Dubai (Rachel instance,
event 772):

1. Operator sent a long, structured Telegram message asking Rachel to
   provision a new fleet instance (Alex Morgan persona brief + bot token +
   instructions).
2. First two triage calls classified it `system` (conf 1.0, 0.95) → routed
   to `claude:opus` → both adapter spawns hit the 300s timeout and were
   re-queued.
3. On the third triage attempt the openrouter classifier (deepseek
   `deepseek/deepseek-v4-flash`) flipped to `unsafe` (conf 0.95) on
   identical content.
4. Gateway dropped the event. Operator received no reply, no rejection
   notice, no audit trail except the log line. From the user's side it
   looked like the bot died.

The classifier is a probabilistic one-shot LLM call. Confident `unsafe`
verdicts on legitimate operator content are a known failure mode of small
gating models, especially on long technical prompts. The current "drop and
hope the user notices" path is unacceptable for an executive-assistant
deployment where every message is supposed to be answered.

`unsafe` is the only triage class with no recourse. Every other class has a
brain. `unsafe` should also have one — a less-restricted fallback brain
that handles edge content the primary classifier flagged.

## Goal

When triage classifies `unsafe`:

1. **If a fallback brain is configured**, route the event to it instead of
   dropping. Skip the primary brain's `triage_routing` map entirely; the
   fallback brain owns this dispatch.
2. **If no fallback is configured**, behavior is unchanged (drop with the
   existing `triage_unsafe` log + audit). Existing deployments keep current
   semantics.
3. The fallback path emits a distinct log line + metric so operators can
   see how often it kicks in. If the fallback also fails or the fallback
   brain itself refuses, log + drop, do not loop.

## Non-goals

- Re-running the primary triage with a second classifier model. That is a
  separate (and weaker) lever; if you want it, file a follow-up. The user
  asked for a fallback **brain**, not a fallback **classifier**.
- Soft-class fallback (e.g. send `analysis` to a different brain when
  primary is busy). Out of scope.
- Per-conversation override or per-channel override. Single global
  `triage_unsafe_fallback_brain` for the first cut.
- A new generic "openrouter brain adapter" that lets any openrouter model
  be a brain. The first usable build can be narrow: a Grok-via-openrouter
  brain. Generalization is a follow-up.

## Configuration

New top-level keys in `gateway.yaml`:

```yaml
triage_unsafe_fallback_brain: openrouter:x-ai/grok-4-fast   # null/absent disables
triage_unsafe_fallback_timeout_seconds: 60                  # optional, default 60
```

`triage_unsafe_fallback_brain` is a brain spec in the same shape as
`default_fallback_brain` and `triage_routing.<class>` (see
`lib/gateway/brain_spec.py`). If the prefix is a brain we don't have an
adapter for, validation fails at startup.

Add to `OWNED_KEYS` in `bin/jc-upgrade` (see PR #42 — the upgrade-merge
spec) so it survives operator upgrades.

## Brain adapter

A new brain `openrouter` (file: `lib/gateway/brains/openrouter.py`) that:

- Reads model from the brain spec's `:<model>` portion (e.g. spec
  `openrouter:x-ai/grok-4-fast` → model `x-ai/grok-4-fast`).
- POSTs to `https://openrouter.ai/api/v1/chat/completions` with the event
  text + the same persona/preamble we render for other brains
  (`render_preamble` in `lib/gateway/context.py`).
- Reads API key from `OPENROUTER_API_KEY` (already present in `.env` on
  every fleet instance — it's the triage credential).
- Wraps response in the standard `BrainResult` shape; sets
  `session_id=None` (openrouter has no persistent session model — every
  call is stateless, which is a cost-correctness tradeoff for the unsafe
  path: lower context fidelity is acceptable when the alternative was a
  silent drop).
- Honors `triage_unsafe_fallback_timeout_seconds`.

The brain adapter is not registered with the normal routing table — only
the unsafe-fallback path uses it. (Generalizing it for `default_brain` /
`triage_routing` is the follow-up; we don't want grok-as-default
side-effects landing in this PR.)

## Runtime behavior

Inside `_maybe_triage` (`lib/gateway/runtime.py:402-482`):

```python
if result.is_unsafe():
    self.log(
        f"triage rejected event id={event.id} as unsafe",
        event_id=event.id,
        kind="triage_unsafe",
    )
    fb_spec = self.config.triage.unsafe_fallback_brain
    if fb_spec:
        self.log(
            f"triage unsafe-fallback id={event.id} routed={fb_spec}",
            event_id=event.id,
            kind="triage_unsafe_fallback",
        )
        # Build a TriageHint that points at the fallback brain. Same
        # shape the rest of the pipeline expects, so dispatch reads it
        # like any other class.
        brain, _, model = fb_spec.partition(":")
        return router.TriageHint(brain=brain, model=model or None,
                                 confidence=result.confidence), False
    return None, True   # existing drop path
return hint, False
```

Two log/metric kinds:

- `triage_unsafe` — original line, always emitted on an unsafe verdict.
- `triage_unsafe_fallback` — emitted only when we actually rerouted. Lets
  the operator distinguish "classifier flagged unsafe AND we dropped" from
  "classifier flagged unsafe AND we redirected to the fallback brain".

The fallback brain dispatches through the normal adapter pipeline, so
adapter timeouts, retries, recovery, and parse-error handling all reuse
existing logic. No special unsafe-only error paths.

## Failure semantics

- **Fallback adapter timeout / non-zero exit:** treated as any other
  adapter failure. The event re-queues; the next attempt re-triages. If
  triage is sticky at `unsafe`, the loop continues until `max_retries`,
  then the event is shelved like any other dispatch failure.
- **Fallback brain refuses (e.g. Grok itself emits a "I won't answer"
  response):** delivered to the user as the brain's text. We don't retry
  with a tertiary model. The user sees the refusal and can rephrase.
- **No fallback configured:** existing behavior — drop, log
  `triage_unsafe`. No regression for instances that don't opt in.

## Test plan

1. **Unit (Python):** `tests/gateway/test_triage_unsafe_fallback.py`
   - With `triage_unsafe_fallback_brain` set, an `unsafe` verdict produces
     a `TriageHint` pointing at the fallback brain, not `None, True`.
   - With it unset, behavior is unchanged.
   - Fallback `TriageHint` carries the right `brain`/`model` fields.
   - `triage_unsafe_fallback` log line emitted exactly once per unsafe
     event when fallback is configured.

2. **Integration:** `tests/gateway/test_runtime_unsafe_dispatch.py`
   - Mock triage backend forced to return `unsafe`. Mock openrouter brain
     returns `BrainResult(response="answered")`. Assert the response is
     delivered to the user.
   - Adapter timeout on the fallback brain: assert event re-queues like
     any other failure.

3. **Smoke (manual on Rachel):** Configure
   `triage_unsafe_fallback_brain: openrouter:x-ai/grok-4-fast`. Send a
   message the deepseek classifier reliably flags `unsafe` (we have the
   2026-05-09 incident as a known repro: the Alex Morgan provisioning
   prompt was reproducibly classified `unsafe` on retry). Assert a reply
   is produced.

## Migration

- Existing instances: no action. Default keeps drop semantics until an
  operator opts in.
- Rachel + fleet rollout: separate operational task, not part of this
  spec. Will land in `gateway.yaml` per-instance.

## Anti-patterns to avoid

- **Don't make the fallback the default.** Adding grok-via-openrouter as
  the unsafe path is fine. Making it the default brain for everyone — or
  silently routing all `analysis` traffic through it — would burn
  openrouter credits and lose Claude's session continuity. Single named
  config key, off by default.
- **Don't mask classifier bugs.** If the fallback path absorbs traffic
  the primary should have routed to a normal brain, the metric will hide
  the fact that the primary triage is mis-classifying. Keep both log
  kinds (`triage_unsafe` + `triage_unsafe_fallback`) so operators can see
  the rate of unsafe verdicts independent of whether they were rerouted.
- **Don't loop into the fallback.** If the fallback brain itself were
  ever pluggable as a triage backend, we'd risk an unsafe-flag-on-unsafe
  loop. Keep the fallback path one-shot: triage runs once per event, and
  the fallback brain dispatch does not re-trigger triage.
- **Don't leak openrouter context across fallback calls.** Stateless API,
  no `session_id` write-back. Every fallback call starts cold.

## Out-of-scope follow-ups

- **Generic openrouter brain adapter** (any model, registered in the
  normal routing table). The narrow first build is grok-only. Generalize
  in a follow-up PR once the unsafe path is proven.
- **Secondary triage classifier.** Separate spec, separate PR. The user's
  ask was for a fallback brain, not a second classifier — but a less
  censoring secondary classifier is also a real lever and worth
  considering once this lands.
- **Per-channel / per-conversation fallback overrides.** Out of scope.
- **User-visible "your message was rerouted" notice.** Considered. Not
  shipping in v1; the brain's reply is enough signal that the event was
  handled. Add later if it's a felt gap.
