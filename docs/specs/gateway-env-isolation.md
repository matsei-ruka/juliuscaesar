# Gateway env-isolation

## Problem

The HOT.md `Known nuisances` register has carried this entry for weeks:

> **CRITICAL env-leak: cross-instance bot impersonation.** `env_value()`
> checks `os.environ` FIRST. Manual watchdog tick from sibling shell
> leaks token → 409 + session bleed. Fix: `env -i` on all manual starts.
> Framework gap pending.

On 2026-05-29 14:00:41Z, Ethan Zhang's gateway was stopped. The
gateway log carried `telegram poll error: HTTP Error 409: Conflict`
lines from before the stop — a sibling instance on the same host was
already polling the same Telegram bot because some operator had run
`jc-gateway start` from a shell that inherited the wrong instance's
`TELEGRAM_BOT_TOKEN`. The current load order
(`os.environ` → `.env`) hands the parent shell's value to the daemon
even when `.env` has the right one, because `env_value()` prefers
`os.environ` for any key the safe-env list considers "reserved" (which
includes every secret).

The "framework gap" is that `jc-gateway start` doesn't sanitize its
parent env before exec — so any leakage from a sibling instance's
shell carries straight through into the daemon. The HOT.md workaround
(`env -i` at start time) only helps the one operator who remembers it.

## Solution

`bin/jc-gateway` adds an env-isolation layer to the `start` path. The
new daemon process launches with a sanitized env: a small whitelist of
runtime variables, plus everything explicitly loaded from
`<instance>/.env`. Any parent-shell token for a sibling instance gets
dropped before exec.

### Module: `lib/gateway/env_isolation.py`

Pure functions, testable without spawning processes:

```python
DANGEROUS_PREFIXES = ("CODEX_", "CLAUDE_")
DANGEROUS_KEYS = frozenset({
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "DASHSCOPE_API_KEY", "OPENAI_API_KEY",
    "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY",
    "COMPANY_API_KEY",
})
WHITELIST_KEYS = frozenset({
    "HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG", "TZ", "PWD", "TMPDIR",
})
WHITELIST_PREFIXES = ("LC_",)

def is_dangerous(key: str) -> bool: ...
def is_whitelisted(key: str) -> bool: ...
def sanitize(parent_env: Mapping[str, str], dotenv: Mapping[str, str]
            ) -> tuple[dict[str, str], list[str]]
```

`sanitize()` returns `(clean_env, stripped_keys)`. Algorithm:

1. Start with `{}`.
2. For each key in `parent_env`: if `is_whitelisted(key)` → copy.
3. Track `stripped = [k for k in parent_env if is_dangerous(k) and k not in dotenv]`
   — for the audit log only; dangerous keys present in `dotenv` are
   overwritten in step 4 anyway and aren't "leaks", they're just being
   replaced.
4. Layer `dotenv` on top (these are the instance's authoritative values).
5. Return.

Notes:
- The whitelist is intentionally tight. Anything not whitelisted and
  not present in `.env` is dropped, even if benign.
- `is_dangerous` covers the prefixes in addition to the explicit set so
  `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, etc. don't leak between siblings.
- The `_RESERVED_INSTANCE_ENV_KEYS` set in `lib/gateway/config.py`
  already lists names that `.env` cannot legally redefine; this module
  is the complementary "what we *let through* from the parent" list.

### `bin/jc-gateway start` change

`cmd_start` already constructs `subprocess.Popen` to fork the daemon.
It currently passes no `env=` arg → `os.environ` inherits in full.
After this change:

```python
from gateway.env_isolation import sanitize
from gateway.config import parse_env_file

clean, stripped = sanitize(os.environ, parse_env_file(instance / ".env"))
proc = subprocess.Popen(..., env=clean, ...)
log_line(instance, f"gateway env-isolated: stripped={len(stripped)} keys, loaded={len(dotenv)} from .env")
```

The audit line lands in `gateway.log` so operators have a record of
which keys got dropped — useful for debugging "the daemon can't see
$FOO" the first time someone hits it.

### `--no-env-isolation` flag

For the rare debugging case where the operator wants the parent env to
inherit (running against a custom DASHSCOPE_BASE_URL, etc.), the flag
preserves `os.environ` entirely and emits:

```
gateway env-isolated: skipped (--no-env-isolation)
```

The flag also propagates through `cmd_restart` so `jc gateway restart
--no-env-isolation` works.

### Acceptance

1. With a parent shell that exports `TELEGRAM_BOT_TOKEN=POISON` and a
   `.env` that sets it to `CORRECT`, the started daemon's env carries
   `CORRECT`. `POISON` is gone.
2. With a parent shell that exports `TELEGRAM_BOT_TOKEN=POISON` and a
   `.env` that does **not** mention it, the daemon's env has no
   `TELEGRAM_BOT_TOKEN`. (Failing closed is correct — the daemon will
   refuse to start the telegram channel rather than poll with a
   sibling's token.)
3. With `--no-env-isolation`, parent env passes through unmodified.
4. The whitelist (`HOME`, `PATH`, `LANG`, …) always reaches the child
   regardless of `.env` contents.
5. The audit line lands in `gateway.log` exactly once per `start` call.
6. Unit tests cover all the above on the `sanitize()` function directly,
   without launching real subprocesses.
