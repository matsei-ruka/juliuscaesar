# Spec: Optional reply footer (model · session · elapsed)

**Status:** Draft
**Date:** 2026-05-07
**Branch base:** `main`
**Owner:** tbd

## Goal

When enabled by config, append a small footer to assistant replies that
records the technical context of the answer:

- Brain + model that produced it (e.g. `claude:sonnet-4-6`)
- Resumed brain session id (truncated, when available)
- Wall-clock seconds between event receipt and reply send

Format (default):

```
⚙️ claude:sonnet-4-6 · sess 7f3a…b41 · 4.2s
```

A single, terse, opt-in footer. Operator-visible debugging surface; not a
product feature.

## Non-goals

- Not a per-channel feature toggle. Single instance-level switch in v1.
- Not a token / cost report. (Token counting is brain-specific and noisy.)
- Not exposed to end users by default. The flag stays off by default.
- Not a replacement for the `📊 <N>k ctx · <P>% cached` session-stats footer
  pattern used in `memory/L1/USER.md` for instance-level signals — that one
  does not exist in the gateway today and is out of scope here.
- Not added to push-marker / canonical-sender flows where the brain already
  pushed its own message bypassing `_deliver_response()` (no place to attach).
- Not added to voice replies. Footer is text-only; TTS pipeline keeps the
  raw text unchanged. (Open question: revisit if operators want it spoken.)

## Current behavior

### Where outbound text is finalized

`lib/gateway/runtime.py` lines `584-621`:

```python
raw_response = result.response or ""
parsed = parse_brain_output(raw_response, event_source=event.source)
...
elif parsed.message:
    if meta.get("was_voice"):
        self._render_voice_reply(parsed.message, meta)
    self._deliver_response(channel, parsed.message, meta)
    self._log_outbound_transcript(event, parsed.message, meta, channel)
```

After this point, delivery flows through `lib/gateway/delivery.py:deliver_response()`
and channel-specific senders (e.g. `lib/gateway/channels/telegram_outbound.py:send_text()`).

### Available signals

- **Brain + model:** `BrainSelection` at `lib/gateway/router.py:28-32` —
  `brain: str`, `model: str | None`. Destructured at `runtime.py:530`.
  After the vision-fallback block at `runtime.py:531-538`, `brain` and
  `model` hold the final routed values used by `invoke_brain()`.
- **Session id:** `BrainResult` at `lib/gateway/brains/base.py:35-40` —
  `session_id: str | None`. Captured at `runtime.py:576`.
- **Inbound timestamp:** `Event.received_at` (ISO 8601 UTC, seconds
  precision) at `lib/gateway/queue.py:32`.

### Existing config conventions

`GatewayConfig` (`lib/gateway/config.py:94-…`) holds top-level toggles. Triage
nests under `TriageConfig` (`lib/gateway/config.py:51-67`). Pattern: typed
frozen dataclass with sensible defaults; loaded via a `_load_*` helper from
yaml; allow-listed in `_validate_raw_config()`.

## Desired behavior

### Config

New nested dataclass `ReplyFooterConfig` on `GatewayConfig`:

```python
@dataclass(frozen=True)
class ReplyFooterConfig:
    enabled: bool = False
    emoji: str = "⚙️"
    show_model: bool = True
    show_session: bool = True
    show_elapsed: bool = True
    session_chars: int = 8        # truncate session id to N chars (with ellipsis)
    separator: str = " · "
```

Yaml shape:

```yaml
reply_footer:
  enabled: true
  emoji: "⚙️"
  show_model: true
  show_session: true
  show_elapsed: true
  session_chars: 8
```

Defaults: `enabled=false`, so existing instances see no change.

### Footer composition

Pure helper in a new module `lib/gateway/reply_footer.py`:

```python
def render_footer(
    cfg: ReplyFooterConfig,
    *,
    brain: str,
    model: str | None,
    session_id: str | None,
    elapsed_seconds: float | None,
) -> str | None:
```

Returns `None` when `enabled=False`, when the message would otherwise be
empty, or when no enabled field has a value to show. Otherwise returns a
single-line string of the form:

```
{emoji} {brain[:model]} · sess {abbrev(session_id)} · {elapsed:.1f}s
```

Composition rules:

- `brain[:model]`: if `model` is set → `f"{brain}:{model}"`; else `brain`.
- `abbrev(session_id)`: first `session_chars` characters + `…` if longer;
  the literal `none` if `session_id is None` and `show_session=true`.
- `elapsed`: rendered with one decimal for `< 60s`, otherwise `mm:ss`.
- Each enabled field that has a value contributes one segment; empty
  segments are skipped (no leading/trailing separators, no double
  separators).
- Telegram MarkdownV2 escaping is handled downstream
  (`telegram_outbound.send_text()` already escapes the full payload). Footer
  must not pre-escape; it returns plain text.

### Where it attaches

In `lib/gateway/runtime.py`, in the `elif parsed.message:` branch
(`runtime.py:612-616`), after the voice render and before
`_deliver_response()`:

```python
elif parsed.message:
    footer = reply_footer.render_footer(
        self.config.reply_footer,
        brain=brain,
        model=model,
        session_id=result.session_id,
        elapsed_seconds=_elapsed(event),
    )
    message_out = parsed.message + ("\n\n" + footer if footer else "")
    if meta.get("was_voice"):
        self._render_voice_reply(parsed.message, meta)   # voice gets the bare text
    self._deliver_response(channel, message_out, meta)
    self._log_outbound_transcript(event, message_out, meta, channel)
```

Notes:

- Voice path (`_render_voice_reply`) keeps `parsed.message` (no footer).
  Telegram still receives the footered text after voice render returns.
- Push-marker path (`runtime.py:600-611`) is **not** footered. The brain
  has already pushed its own message and the gateway has no delivery hook
  there.
- Slash-command early returns (`runtime.py:518`) and inline-override
  responses (`runtime.py:510-511`) are **not** footered. They have no
  triage / brain / session context.
- Transcript log captures the footered message — keeps replay parity.

### Elapsed-time source

`event.received_at` has seconds precision (`now_iso()` strips sub-second
in `queue.py:51-52`). For sub-second resolution, capture
`time.monotonic()` at the start of `_dispatch_inbound()` and read it just
before footer composition. Helper:

```python
def _elapsed(event, monotonic_start: float | None) -> float:
    if monotonic_start is not None:
        return time.monotonic() - monotonic_start
    # fallback: derive from received_at
    return max(0.0, _seconds_since(event.received_at))
```

The dispatch path stores `monotonic_start` as a local at the top of
`_dispatch_inbound()`. No change to `Event`.

### Validation

In `_validate_raw_config()`:

- `reply_footer` must be a mapping when set. Unknown keys → error.
- `enabled` must be bool.
- `session_chars` if set must be `int` in `[3, 64]`.
- `emoji` must be a non-empty string ≤ 8 chars (sanity bound; emoji can be
  multi-codepoint).
- `show_model`, `show_session`, `show_elapsed` must be bool.
- `separator` must be a non-empty string ≤ 8 chars.

## Code plan

Files to add:

- `lib/gateway/reply_footer.py` — `render_footer()` pure function +
  `ReplyFooterConfig` accessors.
- `tests/gateway/test_reply_footer.py` — unit tests for the helper.

Files to modify:

- `lib/gateway/config.py`:
  - Add `ReplyFooterConfig` dataclass.
  - Add `reply_footer: ReplyFooterConfig` field on `GatewayConfig`.
  - Add `reply_footer` to `allowed_top` in `_validate_raw_config()`.
  - Validation rules from above.
  - Loader helper `_load_reply_footer()`, called from `load_config()`.
- `lib/gateway/runtime.py`:
  - Capture `monotonic_start = time.monotonic()` at the top of
    `_dispatch_inbound()`.
  - In the `elif parsed.message:` branch, compose the footered output and
    pass it to `_deliver_response()` + `_log_outbound_transcript()`. Voice
    rendering keeps the bare message.
- `tests/gateway/test_runtime.py` (or a new `test_reply_footer_runtime.py`):
  end-to-end test that the footer appears on a normal text reply and is
  absent when `enabled=false`.

## Test plan

Unit tests for `render_footer()`:

- All segments present → `⚙️ claude:sonnet-4-6 · sess 7f3a4b21… · 4.2s`.
- `model is None` → emits bare brain (`⚙️ claude · sess … · 4.2s`).
- `session_id is None` with `show_session=true` → segment shows `sess none`.
- `session_id is None` with `show_session=false` → segment skipped.
- `elapsed > 60` → `mm:ss` format (e.g. `01:23`).
- `enabled=false` → returns `None`.
- All `show_*` false → returns `None` (no empty footer).
- Long `session_id` truncated to `session_chars` + `…`.

Runtime/integration tests:

- Footer appended to text reply when `reply_footer.enabled=true`.
- No footer when disabled (default).
- No footer on slash-command / inline-override paths.
- No footer on push-marker path.
- Voice path: TTS receives bare text, Telegram receives footered text.
- Transcript log records the footered message.

Config validation:

- Unknown `reply_footer.foo` → error.
- `session_chars=0` → error.
- `emoji=""` → error.

## Rollout plan

**Phase 1 — Land helper + runtime hook + tests.** Default off. No template
change. No doctor change.

**Phase 2 — Doctor surface.** `jc doctor` reports whether `reply_footer` is
enabled.

**Phase 3 — Optional template knob.** `jc setup` could offer
`reply_footer.enabled: true` for new instances during a debugging-friendly
onboarding flow. Off by default.

## Open questions

1. **Voice TTS footer.** Should the footer be spoken when the channel is a
   voice note? Default: no. Operators who want it can set a future
   `reply_footer.voice: true`. Out of scope v1.
2. **Per-channel toggle.** Should the footer respect a per-channel flag
   (e.g. footer only on Telegram DM, not on Slack)? Defer until somebody
   actually asks.
3. **Cost / token segment.** Worth adding `tokens_in / tokens_out / usd` if
   the brain returns it? Brain-specific, noisy. Defer to a separate spec.
4. **Markdown safety.** Should `render_footer()` defensively avoid
   characters that MarkdownV2 escapes oddly (e.g. `_`, `*`, `[`)? The
   current pipeline escapes the whole outgoing string, so the footer is
   safe by construction — but worth a regression test on session ids
   containing `_`.
5. **Resolution of elapsed.** Seconds with one decimal is enough for human
   reading. If we ever want millisecond precision, we already have
   `monotonic_start` in scope.

## Definition of done

- [ ] `ReplyFooterConfig` added to `GatewayConfig`; default `enabled=false`.
- [ ] `lib/gateway/reply_footer.py` ships with `render_footer()` and unit
      tests covering all branches above.
- [ ] `runtime._dispatch_inbound()` captures `monotonic_start` and appends
      the footer in the text-reply branch only; voice / push-marker /
      slash / inline-override paths are unchanged in observable behavior
      when footer is enabled and disabled.
- [ ] Transcript log includes the footer.
- [ ] Config validation rejects malformed `reply_footer` blocks.
- [ ] `jc doctor` reports footer state (Phase 2; not blocking Phase 1).
- [ ] Targeted tests green:

```bash
pytest \
  tests/gateway/test_reply_footer.py \
  tests/gateway/test_runtime.py
```

## Discrepancies with prompt

- Prompt phrased the source of the model as "taken by the triage". In the
  current code, the final model used is on `BrainSelection` produced by
  `router.route()`, which can override the triage-suggested brain via the
  vision fallback at `runtime.py:531-538`. This spec sources the footer
  values from the post-route values, so what is shown is the model that
  actually ran — not the model triage initially suggested. If the explicit
  triage suggestion is preferred, swap `brain`/`model` for
  `triage.brain` / parsed model from `triage` at composition time. Default
  here is "what actually ran" because that is what the user is seeing.
- Prompt did not specify whether to include the footer in the persisted
  transcript. This spec includes it (replay parity); flag if a different
  policy is wanted.
