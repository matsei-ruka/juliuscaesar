---
title: Accountabilities system
section: subsystem
status: active
last_verified: 2026-05-15
verified_by: Rachel Zane
code_anchors:
  - path: lib/gateway/config.py
    symbol: AccountabilitiesConfig
  - path: lib/memory/accountabilities_audit.py
    symbol: append_audit_entry
  - path: lib/health/accountabilities_check.py
    symbol: check_accountabilities
  - path: bin/jc-doctor
  - path: tests/gateway/test_config_env.py
    symbol: AccountabilitiesSchemaTests
  - path: tests/memory/test_accountabilities_audit.py
  - path: tests/health/test_accountabilities_check.py
related:
  - contract/config-and-secret-boundaries.md
  - subsystem/memory-system.md
sources:
  - path: docs/specs/accountabilities.md
    title: Accountabilities spec (P1–P7)
---

# Accountabilities system

## What it is

Opt-in governance layer that lets an instance operator define and track discrete **accountabilities** — named areas of responsibility with explicit scope, stakeholders, cadence, and decision boundaries. Manifests live in L1 memory; detail files in L2. Changes are authority-gated and append-only-audited. `jc-doctor` surfaces health at a glance.

Shipped across 5 phases on branch `spec/accountabilities` (commit `b7148dc`). 31 tests green. P6 (classification telemetry) deferred; P7 (e2e smoke) is manual operator work.

## Key invariants

1. **Feature is opt-in.** `accountabilities.enabled` defaults to `false`. When disabled, every check returns a single `INFO` item — no warns, no noise.
2. **Audit log is append-only, create-on-first.** `append_audit_entry` creates `memory/L2/accountabilities/_audit.md` with frontmatter + table header on first call, then appends one row per subsequent call. Never overwrites.
3. **`tests/health/` must NOT contain `__init__.py`.** pytest prepend mode adds the test subdirectory to `sys.path`; a package init there shadows `lib/health/` and causes silent import of the wrong module. `tests/gateway/` and `tests/memory/` follow the same rule.
4. **Detail files require all 9 sections.** `_detail_has_all_sections()` checks for: Scope, Out of scope, Outputs, Stakeholders, Cadence, Decision boundary, Adjacency notes, Self-check pre-action, Connections to existing constitution. Missing any → `WARN`.
5. **RULES.md check requires "Accountability Principle" AND ≥2 of the 4 engagement levels.** ("Inside", "Adjacent", "Outside", "Delegated"). Rationale: avoids false-OK on empty headers.

## Architecture

```
ops/gateway.yaml
  accountabilities:
    enabled: true
    authority_channel: telegram-primary   # "telegram-primary" | "email" | "none"
    enactment_token: "OK enact"
    authority_email_sender: ""            # required when authority_channel == "email"
         │
         ▼
lib/gateway/config.py → AccountabilitiesConfig (frozen dataclass)
         │
         ├── lib/memory/accountabilities_audit.py
         │     append_audit_entry(instance_dir, AuditEntry)
         │     → memory/L2/accountabilities/_audit.md   (create-on-first, append-only)
         │
         └── lib/health/accountabilities_check.py
               check_accountabilities(instance_dir) → list[HealthItem]
               called by bin/jc-doctor (Python subprocess via shell)
```

**Manifest layout** (all paths relative to `instance_dir`):

```
memory/L1/accountabilities-manifest.md      ← L1 manifest (frontmatter + links to L2)
memory/L1/RULES.md                          ← must contain constitutional section
memory/L2/accountabilities/<slug>.md        ← detail file per accountability (9 sections)
memory/L2/accountabilities/_audit.md        ← append-only audit log
```

## Mini recipe

**Add a new accountability:**

```
1. Append link to memory/L1/accountabilities-manifest.md:
     [Name](../L2/accountabilities/<slug>.md)

2. Create memory/L2/accountabilities/<slug>.md with all 9 required sections.

3. Ensure memory/L1/RULES.md contains "Accountability Principle" + ≥2 engagement level names.

4. Run: jc-doctor
   All accountability items should show ✓ ok.

5. First enactment: call append_audit_entry(instance_dir, AuditEntry(...))
   _audit.md is auto-created on first call.
```

**Add a test for a new health check:**

```
1. Edit tests/health/test_accountabilities_check.py  (NO __init__.py in tests/health/)
2. Use tempfile.TemporaryDirectory() for isolation
3. Call gateway_config.clear_config_cache() in setUp + tearDown
4. Run: pytest tests/health/
```

## Gotchas

- **`__init__.py` in test subdir masks lib package.** If `tests/health/__init__.py` exists, pytest treats it as the `health` package → `from health.accountabilities_check import ...` imports the test dir, not `lib/health/`. Delete it if it appears.
- **Config cache must be cleared between tests.** `gateway_config.clear_config_cache()` in `setUp`/`tearDown` prevents state bleed from one test's `gateway.yaml` leaking into the next.
- **`authority_email_sender` only validated when `authority_channel == "email"`.** Empty string is valid for all other channels. See `_load_accountabilities()` in `config.py`.
- **Manifest link regex is strict.** `_MANIFEST_DETAIL_LINK` matches `[text](../L2/accountabilities/<slug>.md)` — exactly that path prefix. Links with different casing or extra path components won't be parsed as accountability references.

## Open questions / known stale

- **2026-05-15**: P6 (classification telemetry hooks) deferred — not in v1 scope. Will need a new KB entry when implemented.
- **2026-05-15**: P7 (end-to-end smoke test) is manual operator work — scaffold fresh instance, fill ≥3 accountabilities, verify jc-doctor + enactment flow + impersonation defense.

## See also

- `contract/config-and-secret-boundaries.md` — how `gateway.yaml` is loaded and validated
- `subsystem/memory-system.md` — L1/L2 layout this system writes into
- `docs/specs/accountabilities.md` — full spec with all 7 phases
