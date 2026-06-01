# Post-update feature audit + Telegram notifier

## Problem

JC framework releases keep adding opt-in features: actions, accountabilities,
entities, inter-agent protocol, adaptive discovery, voice, email,
self-model, dream, commitments, reengage. Each ships disabled. Operators
don't know:

1. Which features even exist (the changelog is a wall of text).
2. Which ones are off on their instance.
3. Which ones are newly available since their last update.

The result: instances run on a stale subset of capabilities. We surface
the gap actively, once, after each upgrade.

## Solution

A pure-Python audit module + a `jc-features` CLI + a release hook.

### `lib/heartbeat/feature_audit.py`

```python
@dataclass(frozen=True)
class Feature:
    name: str
    status: Literal["enabled", "disabled", "missing"]
    where: str           # file path / yaml key, for the operator's eyes
    hint: str            # one-line description
```

A single function:

```python
def scan(instance_dir: Path) -> list[Feature]:
    """Return one Feature per opt-in capability, in stable order."""
```

Scan covers:

- **Heartbeat builtins** (six entries): `dream_tick`, `self_model_run`,
  `hot_tidy`, `journal_tidy`, `commitments_tick`, `reengage_tick`.
  - "enabled" iff tasks.yaml has `enabled: true` AND the calling user's
    crontab carries a marker block referencing the task (cron line whose
    final argument matches `<task-name>`).
  - "disabled" if the flag is false OR the task is present in tasks.yaml
    but missing from crontab.
  - "missing" if not present in tasks.yaml at all (operator stripped it).
- **Gateway opt-ins** (seven entries): read `ops/gateway.yaml`.
  - `actions.enabled` → name "actions"
  - `accountabilities.enabled` → "accountabilities"
  - `entities.enabled` → "entities"
  - `inter_agent_protocol.enabled` → "inter-agent-protocol"
  - `adaptive_discovery.enabled` → "adaptive-discovery"
  - `channels.voice.enabled` → "voice-channel"
  - `channels.email.enabled` → "email-channel"

Hints are short — one line each — and live next to the scanner so they
co-evolve with the feature itself.

### `<instance>/state/feature-audit-snapshot.json`

```json
{
  "ts": "2026-06-01T07:00:00+04:00",
  "framework_version": "2026.06.01",
  "features": {
    "actions": "enabled",
    "dream_tick": "disabled",
    "...": "..."
  }
}
```

On each scan, the prior snapshot is loaded. A feature is "newly available"
iff its name is absent from the prior snapshot. (Note: a feature flipping
disabled→enabled is **not** new — only first-appearance counts. We track
opt-in *presence*, not change history.)

### `jc features` CLI

```
jc features list [--instance-dir <path>]
jc features notify-disabled [--only-new] [--instance-dir <path>] [--dry-run]
```

- `list` — print a 3-column table (name / status / hint) to stdout. No
  side effects.
- `notify-disabled` — build a Telegram message listing currently disabled
  features (or, with `--only-new`, only those absent from the prior
  snapshot). Pipe through `lib/heartbeat/lib/send_telegram.py`.
- `--dry-run` — print message body to stdout, skip send, skip snapshot
  write.

After a successful send (or dry-run, deliberately, so the operator can
re-run cleanly), the snapshot is updated. Rationale: dry-run is a
preview; a real send is the commitment. If send fails (telegram
unreachable), snapshot is **not** updated — we retry on the next run.

### Message format

MarkdownV2-safe, short:

```
*New JC features available* — 3

• `actions` — supervisor card Stop / Background buttons
• `entities` — typed entity store + migration
• `voice-channel` — voice messages over Telegram

Reply with the feature name to enable, or `skip` to dismiss.
```

`--only-new` filters the bullet list; the heading reads "New JC features
available". Without `--only-new` the heading is "Disabled JC features" and
the bullet list is everything currently off.

If no features qualify, exit 0 silently. Operators don't need a "nothing
to do" ping in their DMs.

### Release hook `updates/releases/2026.06.01.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
# Notify the principal of newly-available features after upgrade.
# Silent if no new features (first run will surface everything — that
# IS the discovery moment).
if [[ -n "${INSTANCE_DIR:-}" ]]; then
    jc features notify-disabled --only-new --instance-dir "$INSTANCE_DIR" || true
fi
```

Failure tolerance: hook is best-effort. A Telegram outage shouldn't break
the upgrade.

## Anti-features

- **No reply handling.** The "reply with the feature name" line is a
  prompt for the human, not a command parsed by a bot. Wiring two-way
  enable-via-reply lives in a later spec; v1 just delivers the visibility.
- **No diff of disabled→enabled transitions.** First-appearance only. A
  feature flipping off in tasks.yaml is the operator's deliberate act; we
  don't second-guess.
- **No scan of every yaml key.** Hard-coded feature table in
  `feature_audit.py`. Adding a new feature = one line in that table. We
  trade dynamism for an auditable list.

## Acceptance criteria

1. `scan()` returns a Feature for every entry in the hard-coded table.
2. A disabled builtin in tasks.yaml resolves to `status="disabled"`.
3. A builtin with `enabled: true` but no cron line resolves to
   `status="disabled"` (the silent-Mikaela case).
4. A first-run snapshot diff reports every currently-disabled feature as
   "newly available" (`--only-new` shows them).
5. After a snapshot is written, the same scan with `--only-new` reports
   nothing.
6. `notify-disabled --dry-run` prints the message body and exits without
   sending or writing a snapshot.

## Tests

`tests/heartbeat/test_feature_audit.py`:

- `test_disabled_builtin_detected` — builtin with `enabled: false` →
  `status="disabled"`.
- `test_enabled_without_cron_is_disabled` — `enabled: true` but no
  matching crontab line → `status="disabled"`.
- `test_enabled_with_cron_is_enabled` — both flag and cron line present →
  `status="enabled"`.
- `test_snapshot_diff_new_features` — second scan with no snapshot file
  shows everything as new; after writing a snapshot, the next scan shows
  none.
- `test_dry_run_does_not_write_snapshot` — verify side-effect isolation.
