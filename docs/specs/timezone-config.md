# Timezone config field with brain-prompt injection

## Status

Draft — 2026-05-08.

## Why

The framework has no canonical place for the instance's operating timezone.
Result: brain replies and heartbeat templates render times in UTC, forcing
the principal to convert mentally to local. The persona-interview asks for
timezone but only writes prose into `memory/L1/USER.md`; nothing operational
reads it.

`lib/heartbeat/runner.py:501` reads `os.environ.get("TZ", "UTC")` — set
nowhere. `bin/jc-setup`'s `ENV_KEYS` does not include `TZ`. Timezone is
**config, not secret** — belongs in `gateway.yaml`, not `.env`.

Two side-effects:

1. The brain reasons about "now" in UTC, so any user request like "remind me
   tomorrow morning" or "schedule for 3pm" lands at the wrong wall clock.
2. Heartbeat task templates that interpolate `{{date}}` / `{{time}}` /
   `{{timezone}}` produce UTC strings — confusing in cron-driven briefings.

## Goal

A first-class `timezone:` top-level key in `gateway.yaml` that:

1. Validates as a real IANA name (e.g. `Asia/Dubai`) via
   `zoneinfo.ZoneInfo(value)`.
2. Loads through the gateway config parser, surfaces as
   `GatewayConfig.timezone`.
3. Is **injected as a dynamic clock block into every brain prompt** so the
   LLM reasons in the user's local timezone, not UTC.
4. Replaces the `os.environ["TZ"]` lookup in heartbeat runner. `{{date}}`
   and `{{time}}` template vars also use the configured TZ.
5. Is prompted for during `jc setup` and `jc upgrade`, with sane default
   detection (`/etc/timezone` → fall back to `UTC`).

## Schema change

`ops/gateway.yaml`:

```yaml
# IANA name (e.g. Asia/Dubai). Used for time injection into brain prompts
# and heartbeat templates.
timezone: Asia/Dubai
```

- Default: `UTC`.
- Validation: `zoneinfo.ZoneInfo(value)` constructed at config load. On
  `ZoneInfoNotFoundError`, raise `ConfigError("timezone: unknown IANA "
  "zone 'Foo/Bar'")`.
- The validator only rejects unknown zones — empty / missing falls back to
  `UTC` via the default-branch.
- Added to the `allowed_top` set in `_validate_raw_config`.
- Surfaced as `GatewayConfig.timezone: str` (frozen dataclass field).

## Runtime injection — clock block

New helper in `lib/gateway/context.py`:

```python
def render_clock(tz_name: str) -> str:
    """Return the dynamic clock block for the configured TZ.

    Evaluated fresh on every call — no caching. ZoneInfo lookups are
    O(1) after first import; the cost is the strftime, which is sub-µs.
    """
    now = datetime.now(ZoneInfo(tz_name))
    iso = now.isoformat(timespec="seconds")
    offset = now.strftime("%z")  # e.g. +0400
    pretty_offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    return (
        "# Current time\n"
        f"{now.strftime('%Y-%m-%d %H:%M')} {tz_name} "
        f"({pretty_offset}, ISO 8601: {iso})"
    )
```

- Importantly **not** stored in the existing `_CACHE` — that cache is keyed
  off L1 file mtimes and would freeze the wall-clock string forever.
- Falls back to `"UTC"` if `tz_name` is empty.

### Where it gets prepended

**Non-Claude brains** (`needs_l1_preamble = True` — codex, codex_api,
opencode, gemini, aider): `lib/gateway/brains/base.py:prompt_for_event`
prepends the rendered clock block to the existing preamble (after the
`_ROLE_PREAMBLE`, before `# Instance memory`). One blank line separator. The
preamble cache in `context.py` stays untouched — the clock block is rendered
each call and concatenated outside the cache.

**Claude** (`needs_l1_preamble = False` — auto-loads `CLAUDE.md`): we cannot
push the clock into the preamble (which would dirty the cached
`CLAUDE.md` view). Instead, prepend a single line above the user message
body:

```
[Current time: 2026-05-08 18:30 Asia/Dubai (UTC+04:00)] <user message>
```

Compact form chosen so resume sessions don't accumulate wildly varying
preambles. The clock line is built in `ClaudeBrain.prompt_for_event` and
inserted just before the `# User message` body.

### Caching

**No caching anywhere.** The clock block must be evaluated each event.
`render_clock` is called from `prompt_for_event`, which is itself called
once per event dispatch. Event dispatch is millisecond-scale; a sub-µs
strftime is irrelevant.

## Heartbeat change

`lib/heartbeat/runner.py:run_task`:

- Replace `os.environ.get("TZ", "UTC")` with the gateway-config-loaded
  timezone (loaded via `gateway.config.load_config_cached(instance_dir)`).
- Replace `ts = dt.datetime.now()` with `ts = dt.datetime.now(ZoneInfo(tz))`
  so `{{date}}` and `{{time}}` substitutions also render in the configured
  TZ. Tests cover both.
- Falls back to UTC if config load fails (e.g. yaml malformed at heartbeat
  time) — heartbeat must not crash on a config typo.

## Setup / upgrade UX

### `jc setup`

After the existing brain selection and Telegram token prompts, add:

```
== Operating timezone ==
Detected: Asia/Dubai (from /etc/timezone)
Timezone (IANA name) [Asia/Dubai]:
```

- Default: read `/etc/timezone` → strip → validate via `zoneinfo`. If
  missing, malformed, or unreadable → fall back to `UTC`.
- Validates the user-entered value via `zoneinfo.ZoneInfo`; on failure,
  reprompt with the error message.
- Writes the answer into `gateway.yaml` (NOT `.env`) by extending
  `render_default_config(timezone=...)`.

### `jc upgrade`

Existing prompt machinery: `prompt "Operating timezone (IANA name)" "$existing_or_default"`.

- Pulls existing value via `yaml_value timezone`. Falls back to
  `/etc/timezone`. Falls back to `UTC`.
- Hitting Enter keeps the current value.
- Writes a `timezone:` line at the top of the regenerated `gateway.yaml`.

## Migration

- Existing instances without a `timezone:` field continue to start. The
  field defaults to `UTC` in `GatewayConfig`. No migration script needed.
- A one-line note added to `templates/init-instance/memory/L1/HOT.md`'s
  bootstrap content under "Known nuisances" → "Times now render in the
  instance timezone (`gateway.yaml: timezone:`); UTC is the fallback for
  legacy instances. Run `jc upgrade` to set yours."

## Test plan

`tests/gateway/test_timezone_config.py`:

- `timezone: Asia/Dubai` loads cleanly; `cfg.timezone == "Asia/Dubai"`.
- `timezone: UTC` is the default when omitted.
- `timezone: Foo/Bar` raises `ConfigError` with `"timezone"` in message.
- `timezone:` appears in `allowed_top` (regression: unknown-key check
  doesn't reject it).

`tests/gateway/test_context_clock.py`:

- `render_clock("UTC")` contains `"UTC"` and a `YYYY-MM-DD HH:MM` token.
- `render_clock("Asia/Dubai")` contains `"Asia/Dubai"` and `UTC+04:00`.
- Two consecutive calls with a sleep in between produce different strings
  if minute boundary crossed (probabilistic — assert that the strings are
  built from `datetime.now(...)` not a frozen value, by monkeypatching the
  clock module).
- `render_clock("Bogus/Zone")` raises (fail-loud at config load is
  guaranteed; render-time it bubbles).

`tests/gateway/test_brain_prompt_clock.py` (new):

- A non-Claude brain (`OpencodeBrain` or any subclass with
  `needs_l1_preamble = True`) gets the clock block as the first thing after
  `_ROLE_PREAMBLE`.
- `ClaudeBrain.prompt_for_event` prefixes the user message with
  `[Current time: ...]` and does not prepend the full clock block to a
  preamble.

`tests/heartbeat/test_runner_timezone.py` (new):

- A heartbeat task with `{{timezone}}`, `{{date}}`, `{{time}}` substitutes
  the gateway-config TZ, not `os.environ["TZ"]`.
- With `timezone: Asia/Dubai` in `ops/gateway.yaml`, `{{timezone}}` →
  `"Asia/Dubai"`, and `{{date}}`/`{{time}}` reflect that zone (verified by
  freezing the clock and checking offset).

`tests/cli/test_jc_setup_timezone.py` (new) — scripted, not interactive:

- Run `jc-setup --defaults --no-wait` against a tmp instance with
  `JC_SETUP_ASSUME_BRAINS=claude`. Verify the resulting `gateway.yaml`
  contains a `timezone:` line whose value is either `/etc/timezone`'s
  contents or `UTC`.

## Anti-patterns

- Don't push `TZ=` into `ENV_KEYS` — that's the `.env` layer (secrets).
  Timezone is config.
- Don't `os.environ["TZ"] = value` at runtime — racy, process-global,
  affects every thread including unrelated subprocesses.
- Don't cache the clock block. Each event must see fresh time.
- Don't hardcode `Asia/Dubai` anywhere — only `UTC` as the framework
  default.
- Don't inject the clock block into Claude's preamble or `CLAUDE.md`. That
  invalidates the auto-load cache and bloats every resume. Inject into the
  user message body instead.
