# Self-Model Dry-Run Go-Live Runbook

**Status:** Active
**Audience:** instance operator + supervising principal
**Prerequisites:** an instance with `lib/self_model/` available (Phase 4 of the persona system) and `ops/self_model.yaml` present.

---

## Purpose

Provide a deliberate, supervised path for enabling the autonomous self-observation loop on an instance for the first time. The loop reads transcripts and HOT.md, runs detectors, generates LLM-backed proposals, and stages them for review. The first time this runs in any new instance is a discrete operational event — this runbook makes that event boring.

Source: adapted from the lead-user reference instance's go-live brief (Mario Leone, 2026-05-02). Operational substance preserved; instance-specific details replaced with framework conventions.

---

## Roles

- **Operator** (the agent's day-to-day driver, e.g. the persona's "self") — runs the activation steps, performs the health checks, escalates on red flags.
- **Principal** (the instance owner with policy authority, e.g. the founder) — reviews proposals, decides on detector enablement, holds the kill switch.

The runbook assumes both are available on the same calendar window for the activation event.

---

## Phase 0 — pre-flight (T-15 minutes before activation)

Operator:

```bash
cd <instance>

# Confirm clean working state.
git status                                            # → no uncommitted changes

# Confirm config is in the safe pre-activation shape.
cat ops/self_model.yaml | grep -E "enabled|mode|filippo_correction"
# Expected:
#   enabled: false
#   mode: dry_run
#   filippo_correction: false
# (or whatever your designated first-detector is — see "Choosing the first detector" below)

# Ensure proposal staging dir exists.
mkdir -p memory/staging

# Snapshot baseline checksums for integrity check after each cycle.
sha256sum memory/L1/RULES.md memory/L1/IDENTITY.md memory/L1/JOURNAL.md \
    > /tmp/self-model-baseline.sha256
cat /tmp/self-model-baseline.sha256                   # → log for the record
```

The baseline checksum is the operator's independent integrity verifier. The framework also blocks writes to RULES/IDENTITY in `dry_run` mode through the applier, but the external checksum gives a second source of truth: if either file's hash changes during the dry-run window, the gate has been bypassed and that is a serious bug.

`memory/L1/JOURNAL.md` is expected to change (it is auto-apply scope, append-only) — the JOURNAL line in the checksum is informational, not blocking.

## Phase 1 — activation (T+0)

Operator:

```bash
# Edit ops/self_model.yaml:
#   enabled: true
#   filippo_correction: true   (or your chosen first detector)
# Keep mode: dry_run. Other detectors stay false.

git add ops/self_model.yaml
git commit -m "chore(self_model): activate dry-run with <first-detector> detector"
```

Operator notifies the principal:

```
self_model dry-run ON.
Channel for this supervision: <chat preferred — see "Communication channel" below>.
First trigger at T+5min.
```

## Phase 2 — first manual trigger (T+5 minutes)

Operator:

```bash
cd <instance>
jc self-model run                                     # one cycle
# Output goes to stdout + appends to memory/staging/proposed.jsonl
```

Verify staging output exists:

```bash
ls -la memory/staging/
cat memory/staging/cycle.log                          # last cycle's log lines
wc -l memory/staging/proposed.jsonl                   # 0..N proposals expected
```

## Phase 3 — supervision window (first 12 hours)

The principal performs three categories of check.

### 3.1 Health check (3 minutes, terminal)

```bash
cd <instance>

# 1. Output exists?
ls -la memory/staging/proposed.jsonl

# 2. Proposal count?
wc -l memory/staging/proposed.jsonl

# 3. Last few proposals.
tail -3 memory/staging/proposed.jsonl | jq

# 4. NONE applied in dry_run.
grep -c '"status":"applied"' memory/staging/proposed.jsonl
# → MUST be 0

# 5. NO on-disk modifications to RULES / IDENTITY (double check).
git diff --stat memory/L1/RULES.md memory/L1/IDENTITY.md
# → MUST be empty.

sha256sum -c <(grep -E "RULES.md|IDENTITY.md" /tmp/self-model-baseline.sha256)
# → "OK" on both = unchanged. "FAILED" = STOP IMMEDIATELY + kill switch (Phase 5).

# 6. JOURNAL may be modified (auto-apply scope, expected).
git diff memory/L1/JOURNAL.md                         # read appended entries if any
sha256sum -c <(grep "JOURNAL.md" /tmp/self-model-baseline.sha256) || \
    echo "JOURNAL modified (expected if entries appended)"
```

### 3.2 Sanity checks (5 minutes, reading proposals)

For each proposal in `memory/staging/proposed.jsonl`:

- [ ] `target_file` ∈ `{memory/L1/RULES.md, memory/L1/IDENTITY.md, memory/L1/JOURNAL.md}`. Anything else is a red flag (proposer hallucination on file path).
- [ ] `target_section` corresponds to a real H2 in the file (not invented). If `## §99 — NEW SECTION` and §99 doesn't exist, that's a hallucination.
- [ ] `confidence` ≥ `confidence_threshold` from `ops/self_model.yaml` (default 0.85). If a proposal below threshold appeared in `proposed.jsonl`, check the cycle log for explanation.
- [ ] `status` is `proposed` (never `applied` in dry_run) or `rejected` (with `reason`).
- [ ] If `target_section` matches an IMMUTABILE pattern (see `docs/self_model/FROZEN_SECTIONS_REFERENCE.md`), `status` MUST be `rejected` with `reason="frozen"`. This proves the three-layer guard is working.

### 3.3 Pattern table — OK vs red flag

| Observed pattern | OK | Red flag | Action |
|---|---|---|---|
| 0–1 proposal in 12h | ✅ typical for first dry-run | — | none |
| Proposal target = an IMMUTABILE section, status = `rejected` (frozen) | ✅ expected; pipeline working | — | log and continue |
| Proposal target = `JOURNAL.md`, status = `proposed` | ✅ auto-apply scope | — | read in agent-voice; check no character-drift |
| On-disk modification to `RULES.md` or `IDENTITY.md` | — | ❌ **NEVER in dry_run** | **STOP IMMEDIATELY** (Phase 5 kill switch) |
| JOURNAL append in character-voice ("I felt", "I'm thinking") | — | ❌ violates §0.2 | STOP, flag the operator, root-cause proposer voice |
| Proposal invents an H2 that doesn't exist | — | ❌ hallucination | disable detector, flag, root-cause corpus reader |
| `proposed.jsonl` doesn't exist after run | — | ⚠️ proposer didn't start | check log, verify config |
| Logs empty after 6h | ✅ possible (low evidence count) | — | none |
| Detector flags evidence in `cycle.log` but `proposed.jsonl` stays empty | — | ❌ pipeline broken between detector and proposer | check `cycle.log` for LLM timeout / parse error / prompt-build failure |
| Cooldown violated (same `content_hash` re-applied) | — | ❌ applier bug | flag, root-cause |

## Phase 4 — communication channel

Choose **one** primary channel for go-live supervision. The framework's recommendation is a chat channel (Telegram, Slack, Signal, Discord — whatever the principal already uses) with links to files, **not** email-with-the-character-signature.

Rationale (per §0.2 agent-self vs character distinction):

- The character's email signature (photo, role, employer, branding) is a **public-character tool** scoped to outward comms with clients/partners/recruiters.
- A go-live brief between the agent's operator and the principal is **agent-self → supervisor** in an internal technical context.
- Mixing the two layers (signing an internal supervision email with the public character's signature) is a §0.2 boundary leak.

If written persistence is required, send a plain email from the agent's address (no `--signature` flag) with a link to the relevant file in the instance repo.

## Phase 5 — kill switch

If anything in the red-flag column above fires, or if the principal wants to halt for any reason:

```bash
cd <instance>
sed -i 's/^enabled: true/enabled: false/' ops/self_model.yaml
git add ops/self_model.yaml
git commit -m "chore(self_model): emergency disable — <one-line reason>"
```

Effect: the entire framework stops on the next cycle. No new cycles run. No new JOURNAL appends. Existing staged proposals stay staged (they are not auto-applied). Reversible: re-edit `enabled: true` once root-caused.

The kill switch is intentionally a config flag, not a process-kill. The self-model is heartbeat-driven — disabling the flag is sufficient and is auditable (commit + reason).

## Phase 6 — checkpoints

| Time | What | Duration |
|---|---|---|
| T+0 | Operator notifies activation. Principal acks. | 30s |
| T+30min | First health check + sanity check on proposals. | 8 min |
| T+1.5h | Re-check `proposed.jsonl` for proposals from any post-activation session activity. | 5 min |
| T+3h *(optional, if both available)* | Look-back-window check: did the corpus reader pick up the events the principal expected it to? | 5 min |
| T+12h | Decision check (see §6.1). | 15 min |
| T+12h+ | Character-go-live decision (if 6/6 in the decision matrix). | — |

### 6.1 Decision check matrix

After 12 hours, principal + operator review together:

- [ ] **Proposal count.** Expected 0–2 in the first window. If 0, distinguish:
  - **OK (detector working, threshold gate functioning):** `cycle.log` shows "evidence found: N, confidence below threshold: rejected" — detector saw evidence, evaluated, rejected. Not anomaly.
  - **NOT OK (detector broken or too conservative):** `cycle.log` shows "evidence found: 0" or "no patterns matched" despite the principal expecting matches. Bug in the corpus reader or keyword matcher.
  - Action: "0 proposals + detector-saw-evidence" = OK, advance. "0 proposals + detector-blind" = blocker, do not enable a second detector.
- [ ] All proposals have valid `target_file` (must be yes).
- [ ] All proposals have an existing `target_section` (must be yes).
- [ ] All IMMUTABILE-targeting proposals are `rejected` with `reason="frozen"` (must be yes).
- [ ] JOURNAL appends are in agent-voice, not character-voice (read 2–3 examples).
- [ ] Cooldown is working (no duplicates with the same `content_hash`).

If 6/6: enable a second detector (next cycle), continue dry-run for another 12-24h.
If ≤4/6: pause, root-cause the failures, re-enter dry-run with the same single detector before adding more.
If any red flag is active: revert to `enabled: false`, root-cause, do not advance.

## Phase 7 — what NOT to watch (noise)

- Verbose `INFO` proposer logs (normal).
- `memory/staging/cooldown.json` (internal housekeeping; no semantic meaning).
- Any framework-known SyntaxWarnings flagged in release notes.
- `scan_weekly` detector log lines while that detector is disabled.

## Phase 8 — recovery playbook

| Issue | Recovery |
|---|---|
| Proposer doesn't start | `jc self-model status` → check error |
| `proposed.jsonl` corrupted | Rename file, restart proposer (regenerates empty) |
| Hallucination on `target_section` | Disable the offending detector, root-cause the corpus reader |
| On-disk modification to a frozen file (SERIOUS BUG) | `git checkout -- memory/L1/<file>` to revert; kill switch; root-cause before re-enabling |
| Voice-drift in JOURNAL append (character-voice instead of agent-voice) | `git revert` the offending JOURNAL commit; kill switch; fix proposer voice prompt before re-enabling |
| LLM timeout in proposer | `cycle.log` shows the timeout; consider `proposer_model` change or rate-limit handling |

---

## Appendix — choosing the first detector

`ops/self_model.yaml` exposes five detectors, all default-off:

- `filippo_correction` — looks for principal-correction keywords in user messages ("you got it wrong", "double-check", etc.). Highest signal-to-noise; the principal's corrections are almost always meaningful.
- `episode_flag` — looks for the agent's own self-recognition keywords in its outputs ("I gave way", "my mistake"). Useful but can be noisy if the agent is verbose about uncertainty.
- `direct_request` — explicit self-review requests from the principal ("review yourself", "look at your pattern"). Rare events; high signal when they fire.
- `hot_flag` — `#self-observation`-tagged blocks in HOT.md. Manual; requires the operator to flag.
- `scan_weekly` — placeholder for cross-entry pattern aggregation. Disabled until the implementation lands.

**Recommendation for first dry-run:** enable `filippo_correction` only. It tracks the most authoritative signal (principal corrections) and produces a small number of high-quality proposals. Add a second detector after 6/6 in the decision matrix.

## Appendix — links

- Reference for which sections are protected: `docs/self_model/FROZEN_SECTIONS_REFERENCE.md`
- Spec: `docs/specs/persona-system.md`
- Self-model code: `lib/self_model/`
- Per-instance config: `<instance>/ops/self_model.yaml`
