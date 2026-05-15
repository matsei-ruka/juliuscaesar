---
title: Adaptive discovery
section: subsystem
status: active
last_verified: 2026-05-15
verified_by: Rachel Zane
code_anchors:
  - path: lib/gateway/config.py
    symbol: AdaptiveDiscoveryConfig
  - path: lib/gateway/config.py
    symbol: ADAPTIVE_DISCOVERY_AUTHORITY_ALIAS
  - path: lib/gateway/context.py
    symbol: render_adaptive_discovery_block
  - path: lib/gateway/brains/claude.py
    symbol: render_adaptive_discovery_block
  - path: lib/memory/scaffolding.py
    symbol: scaffold_adaptive_discovery
  - path: lib/health/adaptive_discovery_check.py
    symbol: check_adaptive_discovery
  - path: bin/jc-doctor
  - path: tests/gateway/test_context.py
    symbol: AdaptiveDiscoveryBlockTests
  - path: tests/health/test_adaptive_discovery_check.py
related:
  - subsystem/accountabilities.md
  - subsystem/entities.md
  - subsystem/inter-agent-protocol.md
sources:
  - path: docs/specs/adaptive-discovery.md
    title: Adaptive discovery spec (P1–P7)
---

# Adaptive discovery

## What it is

Opt-in discipline that asks the agent to distinguish **declared** facts from **inferred** hypotheses, scale inference tolerance to stakes (low → inferred OK; medium → confirm; high → escalate), and apply a conservative default to unknown entities. The constitutional rules live in `RULES.md`; the gateway injects a live reminder block into the brain preamble; entity records carry per-record `knowledge_state`, `classification_confidence`, and `confidence_basis` fields.

Shipped across phases 1–5 on branch `spec/multi-agent-awareness`. Phase 6 (discovery telemetry) deferred. Phase 7 (smoke) verified on a fresh test instance — 3/3 adaptive-discovery checks green.

## Key invariants

1. **Feature is opt-in.** `adaptive_discovery.enabled` defaults to `false`. When disabled, `render_adaptive_discovery_block()` returns `""` and `check_adaptive_discovery()` emits a single `INFO`. The constitutional § section in `RULES.md` is operator-owned and applies whenever the operator keeps it there.
2. **Escalation channel is live.** `high_stakes_escalation_channel` is read on each event. The default alias `authority` resolves to `accountabilities.authority_channel`. When accountabilities are off OR `authority_channel: none`, the live reminder falls back to the literal phrase `the human authority` (doctor warns separately).
3. **Explicit channel slugs must be enabled.** When `high_stakes_escalation_channel` is a concrete slug (e.g., `telegram`), the channel must be in `SUPPORTED_CHANNELS` AND `channel_cfg.enabled: true`. Doctor warns otherwise — `cfg.channels` is always populated with defaults by `load_config`, so dict presence is not a sufficient check.
4. **Three knowledge states are closed.** `declared`, `inferred`, `hybrid`. Validated by the entities subsystem (this discipline reuses those fields).
5. **`confidence_basis` is a one-line free-text justification.** Required when `classification_confidence` is anything other than `low / unknown` defaults. Doctor heuristic: when entities are enabled, ≥80% of records must have a non-empty `confidence_basis`. Threshold is intentionally a heuristic — operators may push higher.
6. **The framework surfaces, it does not enforce.** No runtime parsing of messages for confidence markers, no automatic escalation, no per-attribute provenance validation. The discipline lives in the agent's reasoning.

## Architecture

```
ops/gateway.yaml
  adaptive_discovery:
    enabled: true
    default_unknown_posture: conservative          # closed enum, v1 only
    high_stakes_escalation_channel: authority      # "authority" alias OR concrete channel slug
         │
         ▼
lib/gateway/config.py → AdaptiveDiscoveryConfig (frozen dataclass)
         │
         ├── lib/gateway/context.py
         │     render_adaptive_discovery_block(instance_dir)
         │     → three-line reminder with live channel substitution
         │     in render_preamble() + Claude per-event prefix
         │
         ├── lib/memory/scaffolding.py
         │     scaffold_adaptive_discovery(instance_dir)
         │     → prints the constitutional § snippet for paste into RULES.md
         │     (no file copies — the discipline reuses the entities directory)
         │
         └── lib/health/adaptive_discovery_check.py
               check_adaptive_discovery(instance_dir) → list[HealthItem]
               called by bin/jc-doctor "Adaptive discovery" section
```

**File layout** (all paths relative to `instance_dir`):

```
memory/L1/RULES.md                  ← must contain Authority Awareness / Adaptive Discovery section
                                      with ≥3 of: declared, inferred, three cautions,
                                      discovery protocol, mutual self-disclosure
memory/L2/entities/<slug>.md        ← confidence_basis lives in entity record frontmatter
```

## Mini recipe

**Enable on an instance:**

```
1. Run: jc memory scaffold adaptive-discovery
     Prints the constitutional § snippet (no files written).

2. Paste the snippet into memory/L1/RULES.md under your next free §-number.

3. Flip ops/gateway.yaml:
     adaptive_discovery:
       enabled: true
       high_stakes_escalation_channel: authority   # or a concrete channel slug

4. If using a concrete channel slug, ensure that channel is enabled in channels:.
   If using the alias `authority`, ensure accountabilities.enabled: true and
   authority_channel != none.

5. Fill `confidence_basis` on each entity record (the discipline's data layer).

6. Run: jc-doctor
     All Adaptive discovery items should show ✓ ok.
```

**The live reminder block** (auto-injected):

```
# Adaptive discovery — live reminder
Knowledge states: declared (fact), inferred (hypothesis). Mark every load-bearing claim.
Stakes threshold: low → inferred OK; medium → confirm; high → escalate via <channel>.
Unknown default: formal, no commitments, observe.
```

## Gotchas

- **`cfg.channels` is always populated.** `load_config` seeds defaults for every supported channel even when `gateway.yaml` omits a `channels:` section. The doctor must check `channel_cfg.enabled`, not dict presence — easy to get wrong.
- **`authority` alias survives accountabilities being off.** The render path falls back to `the human authority` so the reminder remains legible; the doctor flags the misconfig separately. Don't expect the reminder to disappear on misconfig.
- **`confidence_basis` ratio is a heuristic.** 80% threshold is intentional — operators may set tighter standards out-of-band. The framework does not enforce per-record requirements; it only flags fleet drift.
- **Constitutional section name is fuzzy.** The doctor accepts either `Authority Awareness` or `Adaptive Discovery` as the section header (case-insensitive). Operators may merge or split as they prefer.
- **Cache invalidation goes via `ops/gateway.yaml` mtime.** Changing `high_stakes_escalation_channel` updates the rendered reminder on the next event without a gateway restart.

## Open questions / known stale

- **2026-05-15**: P6 (discovery telemetry — when did the agent promote `unknown → categorized`, escalate, or hold under pressure) deferred until v1 is in field use.
- **2026-05-15**: `forbid_high_inferred` flag (preventing `high` confidence on `inferred` claims) tracked in spec Open questions.
- **2026-05-15**: Confidence decay (`confidence_until: YYYY-MM-DD` on entity records) tracked in spec Open questions.

## See also

- `subsystem/entities.md` — confidence_basis discipline operates over entity records
- `subsystem/inter-agent-protocol.md` — peer identity verification uses the discovery protocol
- `subsystem/accountabilities.md` — the alias `authority` resolves to accountabilities.authority_channel
- `docs/specs/adaptive-discovery.md` — full spec with all 7 phases
