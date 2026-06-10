# Spec stub — config schema as single source of truth (audit feature 7, full scope)

**Status:** deferred from Phase 4 (`jc-audit-phase4-remaining.md`). The Phase 4
slice shipped: no silent YAML fallback, nested triage validation, supervisor
section validation, explicit-zero preservation, `triage_cache_ttl_seconds`
drift fix, validated atomic `jc-supervisor` toggle.

**Remaining problem:** the schema is still implicit and duplicated across
`config.py` (validator + N loaders with separate default literals),
`config_writer.py`, `bin/jc-supervisor`, and the watchdog's third YAML parser
(`lib/supervisor/config.py:_parse_yaml`). Every new writer tool re-creates the
writer/validator drift class until one schema object is consumed by all of
them.

**Sketch:**
- One declarative schema module (`lib/gateway/config_schema.py`): key paths,
  types, ranges, defaults, deprecations. Validator, loaders, and writers all
  generated from / checked against it.
- Defaults defined once (kills the duplicated literals drift, audit G-P3).
- Brain model suffix validation against per-brain alias tables.
- Watchdog `_parse_yaml` replaced by a read-only consumer of the same module.
- Migration: schema asserts parity with current validator on the corpus of
  fleet configs before the old paths are deleted.

**Also deferred here:**
- Feature 4 follow-up: SIGHUP live channel rebind (running channels keep
  constructor-captured config).
- Feature 5 follow-up: opt-in live invoke probe (`jc doctor --probe-brains`).
- Feature 9 follow-up: bash-watchdog state out of `/tmp` (needs state
  migration + fleet rollout step).
