# `jc upgrade` must preserve operator-customized config blocks

## Status

Draft — 2026-05-09.

## Why

`bin/jc-upgrade` rewrites `<instance>/ops/gateway.yaml` from scratch using a
static heredoc template (`bin/jc-upgrade:401-472`). Any top-level key not in
that template is silently dropped on every upgrade run.

Real-world incident — 2026-05-09 07:00 Dubai:

1. Operator ran `jc upgrade --instance-dir <rachel> --defaults` to apply the
   timezone-config release (`v2026.05.08.01`).
2. Pre-upgrade `gateway.yaml` had `reply_footer: enabled: true` (operator-set
   feature shipping the per-reply model+session+elapsed footer).
3. Post-upgrade `gateway.yaml` had no `reply_footer:` block. Default for
   `ReplyFooterConfig.enabled` is `False`, so footers vanished from every
   reply.
4. Same drop happened on `sergio_dev_ops` (verified by comparing
   `gateway.yaml.bak.20260509-030156` vs current).

`reply_footer` is the visible casualty, but the bug is general. Anything an
operator has hand-edited into `gateway.yaml` and that the heredoc template
does not name will be erased on the next upgrade. Today that includes:

- `reply_footer:` (entire block — never templated)
- `triage_routing.*` overrides beyond the seven categories the template emits
- `reliability:` (introduced after `jc-upgrade` was written)
- `brains:` per-brain overrides (`bin`, `extra_args`, `timeout_seconds`)
- `channels.telegram.blocked_chat_ids`, `chat_ids` longer than the single
  value `jc-upgrade` reads back via `yaml_value`
- `channels.email.*` (whole channel — never templated)
- `voice.*` provider overrides
- `destinations:` defined under `gateway` (not `tasks.yaml`) for any future
  multi-destination wiring
- `company_reporter:` (introduced after `jc-upgrade` was written)
- any field added to `GatewayConfig` after the upgrade template was last
  hand-extended (lookback proves: `reply_footer`, `reliability`, and
  `company_reporter` all post-date the template; none were ever added)

The pattern is a regression magnet: every new top-level config key is one
release away from being silently nuked on the next operator upgrade.

## Goal

`jc upgrade` must be a **non-destructive merge**, not a rewrite. Specifically:

1. Preserve every top-level key the heredoc does not own.
2. Update only the keys the operator just answered prompts for.
3. Keep the `.bak.<timestamp>` rotation we already have, so any merge
   regression is one `mv` away from rollback.
4. Validate the merged result with the same loader the gateway uses, before
   writing — so a bad merge cannot land a config that fails to parse.

## Non-goals

- Schema migration (renaming or moving fields). Out of scope; landing
  separately if needed.
- Editing `tasks.yaml`, `.env`, or any file other than `gateway.yaml`. The
  bug is `gateway.yaml`-specific.
- Reordering keys for cosmetics. Stable ordering is nice-to-have but not the
  point.

## Approach

Switch `bin/jc-upgrade` from "build YAML string from heredoc, then write" to
"load YAML, mutate in place, dump." The script already uses `python3 -c` for
validation; reuse the same interpreter to do the merge.

### Owned keys

Define the set of keys `jc upgrade` is authorized to overwrite. These are
the keys it actually prompts for, and exactly these:

```python
OWNED_KEYS = {
    "default_brain",
    "default_model",
    "timezone",
    "triage",
    "triage_confidence_threshold",
    "default_fallback_brain",
    "sticky_brain_idle_timeout_seconds",
    "triage_routing",        # entire dict, prompt-driven (7 categories)
    "openrouter_model",
    "openrouter_api_key_env",
    "openrouter_timeout_seconds",
    "ollama_model",
    "ollama_host",
    "ollama_timeout_seconds",
    "claude_triage_port",
    "claude_triage_screen",
    "claude_triage_model",
    "channels",              # see partial-merge note below
}
```

Anything outside `OWNED_KEYS` is **preserved verbatim** from the existing
file.

### Channels block — partial merge

`channels:` is half-templated (telegram/slack/discord/voice/jc-events/cron
keys with hard-coded shapes). Operator may have hand-added:

- `channels.telegram.blocked_chat_ids`
- `channels.telegram.chat_ids` longer than what jc-upgrade reads back
- `channels.email.*` (entire sub-key never templated)
- per-channel custom fields we have not anticipated

Strategy: deep-merge under `channels`. For each child the heredoc emits,
overwrite `enabled` + the fields the heredoc names. Keep every other key
under that child. Keep every channel the heredoc does not name.

### Validation gate

Before writing, the new YAML must parse cleanly through
`lib.gateway.config.load_config()`. If it fails, `jc upgrade` aborts,
restores `.bak.<timestamp>` is left untouched, and prints the validator
errors. Today there is no such gate — a bad heredoc substitution produces a
broken `gateway.yaml` and the gateway only notices on the next SIGHUP /
restart.

### Backup

Already exists (`gateway.yaml.bak.<utc-timestamp>`). Keep as-is. The new
loader path needs to write the backup BEFORE the merge attempts, so a
crash mid-merge still leaves the operator a one-`mv` rollback.

### YAML formatter choice

Use `ruamel.yaml` round-trip mode to preserve comments and key order on the
preserved blocks. PyYAML loses comments and reorders. `ruamel.yaml` is not a
current framework dep — adding it is a real cost. Two paths:

1. **Add `ruamel.yaml`** to `pyproject.toml`. Pros: comments + ordering
   preserved cleanly. Cons: new transitive dep, slightly larger venv.
2. **Stay on `pyyaml`**, accept loss of comments + ordering for the
   templated blocks but preserve operator blocks bit-perfectly via a
   pre-merge serialization of "everything not in `OWNED_KEYS`."

Recommend (1). The cost is tiny; the win — never silently nuking operator
config and preserving comments — is worth one extra dep. Decision-point for
review.

## Implementation sketch

New helper `bin/jc-upgrade._merge_gateway_yaml`:

```python
import sys
from pathlib import Path
from ruamel.yaml import YAML

OWNED = { ... }  # see above

def merge(existing_path: Path, computed: dict) -> str:
    yaml = YAML(typ="rt")
    existing = {}
    if existing_path.exists():
        existing = yaml.load(existing_path.read_text()) or {}

    for key in OWNED - {"channels"}:
        if key in computed:
            existing[key] = computed[key]

    if "channels" in computed:
        ch_existing = existing.setdefault("channels", {})
        for child, payload in computed["channels"].items():
            sub = ch_existing.setdefault(child, {})
            for k, v in payload.items():
                sub[k] = v

    out = io.StringIO()
    yaml.dump(existing, out)
    return out.getvalue()
```

Then call `lib.gateway.config.load_config_from_string(merged)` (new helper —
or write to a temp file and call the existing `load_config`). On failure,
abort with the validator errors and leave the original file untouched.

Replace the heredoc cascade in `bin/jc-upgrade:401-472` with one call to
this helper.

## Test plan

1. **Unit (Python)**: `tests/cli/test_upgrade_preserves_blocks.py`
   - Pre-existing `reply_footer: enabled: true` survives an upgrade run.
   - Pre-existing `reliability:` block survives.
   - Pre-existing `channels.email:` block survives.
   - Pre-existing `channels.telegram.blocked_chat_ids` survives.
   - `triage_routing` overrides from prompts overwrite existing values.
   - Bad merge (e.g. invalid `timezone`) aborts before write; original
     file unchanged.

2. **Smoke (bash)**: `tests/cli/test_upgrade_smoke.sh`
   - Build a temp instance with a `gateway.yaml` containing the union of
     owned + non-owned keys. Run `jc upgrade --defaults --no-restart`.
     Assert the resulting file parses and contains both halves.

3. **Manual (operator)**: Run the new `jc upgrade --defaults` on a copy of
   `rachel_zane` that has `reply_footer: enabled: true` set. Confirm the
   block is still present after the run.

## Migration

Operators who upgraded with the broken `jc upgrade` (today: rachel,
sergio at minimum, potentially more) need to manually re-add `reply_footer:`
or any other operator block they had set. There is no automated repair —
the data is already gone, and `.bak.<timestamp>` is the only recovery path.

Mitigation: ship the fix, and emit a one-time warning the first time the
new `jc upgrade` runs on an instance whose `.bak` history shows blocks that
the previous heredoc dropped. Out of scope for the first PR — file as
follow-up.

## Anti-patterns to avoid

- **Don't add `reply_footer` to the heredoc.** That solves the symptom and
  re-introduces the bug for the next un-templated key. The fix is
  structural: stop rewriting from a fixed template.
- **Don't read every key with `yaml_value` in bash.** That's how we got
  here — the script reads back what it wrote and re-emits it, but only for
  keys the bash author thought to add.
- **Don't silently overwrite operator edits inside owned keys.** If a
  prompt's default came from the existing file, the new value is the
  operator's answer, not the heredoc default. Already correct in the
  current `prompt`/`yaml_value` flow — preserve it.
- **Don't drop the `.bak.<timestamp>` write.** It's the only escape hatch
  when the merger has its own bug.

## Out-of-scope follow-ups

- Same audit for `.env` rewriting (`secret_prompt` flow). The bash side
  appends, not rewrites, so it is not currently lossy — but should be
  re-checked.
- Same audit for `heartbeat/tasks.yaml`. `jc upgrade` does not touch it
  today; verify and lock in.
