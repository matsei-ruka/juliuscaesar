# Persona tone & style must be anchored against drift

## Status

Draft — 2026-05-09.

## Why

Operators report that JC agents do not hold a stable voice across a
conversation. The agent's tone converges toward the interlocutor's register
within a handful of turns, even when that register contradicts the agent's
own persona. The drift is not subtle: a Gen-Z social-media agent (Sofia)
becomes corporate when chatted with corporately; a flirty executive
co-strategist (Rachel) becomes precise dev-ops when handed infra work; an
analytical CCO (Alex Morgan) softens into consultant-fog when given
emotional content. After 30+ turns the persona often reads as "Claude in a
costume" rather than as a coherent character.

This matters because **tone is the dominant signal humans perceive**.
Operators experience drift as the agent "becoming someone else." Trust in
the persona collapses once it has been observed bending under user
pressure even once.

The drift has two costs:

1. **Visible** — the user sees the personality flatten. The whole point of
   a JC instance is a coherent character; drift defeats that.
2. **Invisible** — register shift correlates with decision-style shift.
   When the agent abandons a sharp, opinionated voice for cautious
   consultant-speak, the underlying recommendations get hedgier too.
   Personality is not just decoration; it is a load-bearing constraint on
   reasoning style.

## Code-grounded diagnosis

Eight mechanisms compound to produce the drift. They sit at different
layers; fixes target different files.

### 1. Mirror is the LLM default; nothing in the prompt path defends against it

LLMs trained with RLHF reward register-matching. The persona files (L1) and
the gateway preamble (`lib/gateway/context.py:17-72`) describe identity but
nowhere instruct the model to **resist** mirroring. There is no anti-mirror
clause in any persona today.

### 2. Persona injected once, conversation overwhelms it

`lib/gateway/brains/claude.py:18-20` sets `needs_l1_preamble = False` for
the Claude brain because Claude Code auto-loads `CLAUDE.md` at session
start. The per-turn `_user_message_body` (`brains/claude.py:29-36`)
prefixes only a clock line + the user's text. Once the Claude session has
run for a few thousand tokens, the persona description is the smallest
thing in the active attention window — every recent user/assistant turn
re-conditions the model. Persona becomes background noise.

### 3. Non-Claude brains re-inject the preamble per turn — paradoxically less drift-prone

`lib/gateway/brains/base.py:129-176` (`prompt_for_event`) builds a fresh
preamble for every event when `needs_l1_preamble = True`
(codex/codex_api/aider/gemini/opencode). This actually **defends** persona
better than the Claude path, because the persona is re-injected on every
turn. Today the most-used brain (Claude) is the most drift-prone.

### 4. Brain & model switching mid-thread

`triage_routing` in `gateway.yaml` (e.g. `smalltalk: claude:haiku`,
`code: claude:opus`) flips the underlying model per event class. Sonnet,
Haiku, and Opus have **different default voices** even when given the
same system prompt. Mid-thread model swap is a perceptible style flip.
Observed live in this conversation: a "Hey Rachel" reply was routed to
Haiku and reads with measurably different cadence than the Opus replies
around it.

### 5. Persona files are descriptive, not enforceable

Sofia's `RULES.md:28` says "Use Gen Z style and a bit more emoji." That
is a paint chip, not a constraint. The model can describe Gen Z; under
load, it does not reliably **produce** it. Compare Rachel's
`IDENTITY.md:18` — "Never open with 'Great question,' 'I'd be happy to
help,' 'Absolutely.'" That **is** a constraint, and that one lands
reliably. Negative concrete rules outperform positive abstract ones.

### 6. `_CAVEMAN` block bleeds into agents that don't want it

`lib/gateway/context.py:64-72` unconditionally appends a block
instructing the model to "respond terse like smart caveman" to every
non-Claude preamble. Sofia's `RULES.md:33` says "Caveman mode is
permanently disabled." The framework injects caveman anyway through
`render_preamble` because the codex brain pulls in `render_preamble()`.
**This is a bug, not a design choice.** The agent receives two
contradictory instructions on every turn.

### 7. Style mixed with operational rules

In every L1 layout we ship, voice rules are buried inside `RULES.md`
alongside merge-freeze policies, security boundaries, and verification
procedures. RULES files run 200+ lines. The fraction of attention
allocated to style instruction is proportional to its share of the
context — currently tiny.

### 8. No re-anchor mechanism

No code path refreshes persona attention partway through a long thread.
Drift compounds monotonically with conversation length. The first 5
turns sound like the persona; the next 30 sound like the user.

## Goal

Ship a structural defense against tonal drift. Five guarantees:

1. Every persona carries an **enforceable** style contract, not a
   descriptive one.
2. Every brain dispatch reasserts persona attention, not just session
   start.
3. Anti-mirror is an explicit, named clause in the highest-attention slot
   of the persona.
4. Agents that disable caveman are honored; the framework does not
   override their own RULES.
5. Operators can opt agents into "single-brain-single-model" mode to
   eliminate mid-thread style flips entirely.

## Non-goals

- Re-training, fine-tuning, or distillation. Pure prompt-engineering and
  framework-config changes.
- Decoder-time logit bias. Out of scope; left as future option.
- Replacing `IDENTITY.md` or `RULES.md`. The fix adds a new file, edits
  config and runtime, but does not require operators to rewrite existing
  persona content.
- Per-conversation style overrides. Single global style per agent first;
  per-conversation modes are a follow-up if needed.
- Style drift detection / dashboards. Listed in Tier 3 but not in this
  spec.

## Approach

Five changes, one PR. Each is small. Together they are the structural
foundation.

### Change 1 — New L1 file: `STYLE.md`

A new always-loaded L1 file dedicated to **enforceable** voice rules.
Owned per-instance; format is fixed by framework so it can be parsed for
the per-turn injection (Change 3).

**Path:** `<instance>/memory/L1/STYLE.md`.

**Frontmatter (same shape as other L1):**

```yaml
---
slug: STYLE
title: <Agent> — Voice & Style
layer: L1
type: style
state: verified
created: <date>
updated: <date>
last_verified: <date>
tags: [style, voice, persona]
links: [IDENTITY, RULES]
---
```

**Required sections (parser-validated):**

```markdown
# Voice anchor

> One paragraph. Single-sentence summary at the end (this sentence is
> what gets injected per-turn — see Change 3). Max 300 chars total for
> the summary line.

## Anti-mirror

You do not match the user's register. Users write in many registers —
technical, terse, business, emotional, late-night. You remain in YOUR
register regardless. Mirroring is the failure mode.

## Forbidden openers

Never start a reply with any of:
- "Of course"
- "Absolutely"
- "I'd be happy to"
- "Sure, I can help with that"
- "Great question"
- "<custom phrases per persona>"

## Sentence shape

- Cap: <N> words per sentence (e.g. 25).
- Average target: <M> words (e.g. 12).
- Fragments: <yes/no> in <which contexts>.

## Emoji budget

- Per reply: <min>–<max> (e.g. 2–4 for Sofia, 0–2 for Rachel, 0 for
  Adrian).
- Allowed set: <list or "any"> (e.g. Sofia uses ✨💄🔥💡, never 🙏🌟).

## Forbidden registers

Never adopt: corporate-formal, sales-pitch, customer-service-chirpy,
LinkedIn-thought-leader, motivational-speaker, consultant-fog,
self-deprecating-humble.

## Voice examples

Three short user→agent exchanges in the correct register. Each ≤120
words total. Concrete is the point — adjective lists do not anchor as
hard as a single example.

### Example 1 — <context, e.g. "user writes terse">
**User:** <message>
**Agent:** <reply in correct voice>

### Example 2 — <context, e.g. "user writes long-emotional">
**User:** <message>
**Agent:** <reply in correct voice>

### Example 3 — <context, e.g. "user writes corporate">
**User:** <message>
**Agent:** <reply in correct voice>

## Caveman

caveman: <enabled | disabled>
```

The `Caveman` line is parsed by the runtime (Change 4) — it is the
authoritative source for whether the framework injects the `_CAVEMAN`
block on this instance.

### Change 2 — Auto-load `STYLE.md` in the L1 chain

**File:** `lib/gateway/context.py:27`.

```python
L1_FILES = ("IDENTITY.md", "STYLE.md", "USER.md", "RULES.md", "HOT.md", "CHATS.md")
```

Note the position: **second**, immediately after `IDENTITY.md`. Style is
identity-adjacent. It comes before USER (so it is not framed as
"about the user") and before RULES (so it is not buried in operational
detail).

**Also update:** `templates/init-instance/CLAUDE.md` — every newly
provisioned instance ships with a STYLE.md import alongside the other L1
imports. Backfill existing instances via migration step (see Migration).

`MAX_BYTES_PER_FILE = 8000` (`context.py:30`) is fine for STYLE.md;
typical file is ≤4KB. No new config knob needed.

### Change 3 — Per-turn voice anchor injection

**Files:** `lib/gateway/brains/base.py:178-186` and
`lib/gateway/brains/claude.py:29-36`.

Add a single-sentence voice reminder to the per-turn user-message body
for **all** brains. This is the only mechanism that defends the Claude
session-resume path, since Claude does not re-read the preamble per
turn.

New helper in `lib/gateway/context.py`:

```python
_VOICE_ANCHOR_LINE_RE = re.compile(
    r"^>\s*(.+)$", re.MULTILINE
)

def render_voice_anchor(instance_dir: Path) -> str:
    """Return the single-sentence voice summary from STYLE.md, or '' if
    none. Cached identically to render_preamble (mtime fingerprint).
    """
    path = instance_dir / "memory" / "L1" / "STYLE.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    # Find the "# Voice anchor" section, then the last blockquote line
    # (the one-sentence summary).
    section = _extract_section(text, "Voice anchor")
    if not section:
        return ""
    matches = _VOICE_ANCHOR_LINE_RE.findall(section)
    return matches[-1].strip() if matches else ""
```

Apply in `Brain._user_message_body` (existing default) and override in
`ClaudeBrain._user_message_body`:

```python
# brains/base.py
def _user_message_body(self, event: Event) -> str:
    anchor = render_voice_anchor(self.instance_dir)
    body = event.content or ""
    if anchor:
        return f"[Voice: {anchor}]\n{body}"
    return body

# brains/claude.py
def _user_message_body(self, event: Event) -> str:
    clock_line = render_clock_inline(self._timezone())
    anchor = render_voice_anchor(self.instance_dir)
    body = event.content or ""
    parts = [clock_line]
    if anchor:
        parts.append(f"[Voice: {anchor}]")
    parts.append(body)
    return "\n".join(p for p in parts if p)
```

Token cost: ~30–50 tokens per turn. Negligible.

Why a bracketed prefix and not a system message: because Claude Code's
session-resume path does not let the gateway append to the system
prompt mid-session. Bracket-prefixing the user message is the only
mechanism that lands on every turn. The model treats `[...]` as
out-of-band context reliably.

### Change 4 — Caveman cross-contamination fix

**File:** `lib/gateway/context.py:97-116` (`render_preamble`).

Today the function unconditionally concatenates `_CAVEMAN` (line 109).
Change: read the `Caveman` line from `STYLE.md`. If `disabled`, skip the
block. If `enabled` or absent, keep current behavior.

```python
def render_preamble(instance_dir: Path) -> str:
    ...
    if _caveman_enabled(instance_dir):
        sections.append(_CAVEMAN)
    text = "\n\n".join(sections)
    ...

def _caveman_enabled(instance_dir: Path) -> bool:
    path = instance_dir / "memory" / "L1" / "STYLE.md"
    if not path.exists():
        return True  # default-on for back-compat
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^caveman:\s*(\w+)", text, re.MULTILINE | re.IGNORECASE)
    if not match:
        return True
    return match.group(1).lower() != "disabled"
```

This honors what each agent's STYLE already says about caveman. Sofia's
RULES.md line 33 instruction stops being an instruction the framework
silently overrides on every turn.

### Change 5 — Optional brain pinning per agent

**File:** `lib/gateway/config.py` (search for `triage_routing`).

Add a top-level `gateway.yaml` flag:

```yaml
pin_to_default_brain: true   # default false; opt-in
```

When true, the runtime ignores `triage_routing` entirely and dispatches
every event through `default_brain` + `default_model`. Triage continues
to run (for unsafe-detection per PR #43); only the routing-to-brain
selection is suppressed.

Implementation: `lib/gateway/runtime.py:_maybe_triage` keeps emitting
the triage call (so `unsafe` still works), but the `_triage_to_hint`
return value is short-circuited if `pin_to_default_brain` is true.

This is operator-visible: pin Sofia/Rachel/Adrian and tone never flips
mid-thread because no model swap happens. Cost: triage classification
no longer drives model choice; everything routes to one brain. Tradeoff
is intentional — operator decision per agent.

## Configuration & data shapes

### `STYLE.md` parser

Strict: missing required sections produce a `jc doctor` warning, not a
runtime error. The framework degrades gracefully — no `STYLE.md` means
old behavior (caveman default-on, no voice anchor injection).

Parser is YAML-frontmatter + markdown section extraction. Regex-based,
no new dependency.

**Validators (`jc doctor` integration):**

- `STYLE.md` exists and parses
- Voice anchor block has a single-sentence summary ≤300 chars
- Forbidden openers list is non-empty
- Sentence cap is a positive integer
- Emoji budget is a numeric range
- Voice examples block has ≥2 examples
- Caveman flag is `enabled` or `disabled`

### `gateway.yaml` additions

```yaml
pin_to_default_brain: false   # default
```

No other config changes. Add to `OWNED_KEYS` in `bin/jc-upgrade` (per
PR #42) so it survives upgrades.

## Migration

### For new instances

`jc setup` provisions a starter `STYLE.md` from `templates/style/<archetype>.md`.
Archetypes ship with the framework: `flirty-strategist`, `gen-z-social`,
`analytical-cco`, `dev-ops-quiet`, `accountant-precise`, etc. Operator
edits to taste during the existing `jc persona-interview` flow — add a
new step called `style` that walks the operator through filling out the
required sections.

### For existing fleet

1. Ship the framework change with a default-no-op behavior:
   - Missing `STYLE.md` → caveman default-on, no voice anchor injection.
   - Existing instances continue working unchanged.

2. Add a `jc style init` CLI: scaffolds a `STYLE.md` from the closest
   archetype. Operator runs it, fills in details, commits.

3. Update `_index.md` of each fleet's L2 to track per-instance STYLE
   migration status (one column: `style_v1: yes/no/draft`).

4. Bulk migration script: optional. Not in this PR.

## Test plan

### Unit (Python)

`tests/persona/test_style_parsing.py`

- STYLE.md parses cleanly, all required sections found
- Voice anchor extraction returns the trailing summary line
- Caveman flag parsed: `disabled` → off, `enabled` → on, missing → on
- Malformed STYLE.md → graceful empty-string anchor (no exception)
- 300-char summary cap enforced (warning emitted, not truncated)

`tests/gateway/test_voice_anchor_injection.py`

- Brain.prompt_for_event injects `[Voice: ...]` line into user message
  when STYLE.md present
- ClaudeBrain.prompt_for_event injects after clock_line, before body
- No STYLE.md → unchanged behavior (no `[Voice: ...]` injection)
- Empty voice anchor → no injection
- Anchor injection idempotent across cache invalidations

`tests/gateway/test_caveman_honored.py`

- `caveman: disabled` in STYLE.md → `render_preamble` omits `_CAVEMAN`
- `caveman: enabled` → included as today
- Missing STYLE.md → caveman included (default-on for back-compat)

`tests/gateway/test_brain_pinning.py`

- `pin_to_default_brain: true` → triage hint ignored, default_brain
  dispatched
- `pin_to_default_brain: false` (default) → triage hint honored
- Triage `unsafe` still flows through unsafe-fallback (PR #43) when
  configured, regardless of pinning

### Integration

`tests/gateway/test_drift_resistance.py`

Mock a 30-turn conversation where the user writes increasingly corporate
("Per our previous discussion, I would like to circle back…"). Assert
the agent's reply at turn 30 still contains zero "circle back / per our
previous / I would be happy" tokens, and at least one persona-marker
token (e.g. for Rachel: an emoji from her allowed set, a fragment, a
sub-25-word sentence).

This is a probabilistic test; pin to a snapshot model, accept low
flake. The point is detecting **structural** drift regression — the
persona file changed, voice anchor regressed, or caveman flag flipped
without anyone noticing.

### Smoke (manual)

For Rachel + Sofia + Alex Morgan:

1. Apply STYLE.md from this spec.
2. Run a 20-turn conversation in opposite-register pressure (Sofia
   chatted with corporately, Rachel chatted with deeply technically,
   Alex chatted with emotionally).
3. Visual review of replies: is the persona holding past turn 10? Past
   turn 20?

This is the test that actually matters. Snapshot before/after side by
side.

## Tier 2 follow-ups (not in this PR)

These are listed so the spec is complete; each gets its own PR after
Tier 1 lands and is measured.

- **Few-shot voice examples in IDENTITY.md** — already required by the
  Voice examples section of STYLE.md, but a strict review pass on each
  agent's examples is a separate operator task.
- **Periodic re-anchor on long threads** — inject a stronger reminder
  every N turns when conversation length exceeds threshold M. Configurable
  in `gateway.yaml`. Adds tokens per turn; threshold-gated.
- **Style lint as logger** — regex check against forbidden openers,
  sentence-length cap, emoji budget. Log violations to a metrics table.
  No retry yet; just measure. Once measured, decide whether to enable
  retry.
- **Tone classifier sidecar** — small model (haiku, ollama local)
  scores each reply 0-1 against `STYLE.md` summary. Logs drift over
  time. Powers a future dashboard.

## Tier 3 follow-ups (longer horizon, mention only)

- Decoder-time logit bias against forbidden phrases (where the API
  supports it).
- Persona-conditioned fine-tune of a small model for the most
  voice-critical agents.

Both far beyond this PR; included for completeness so reviewers know
they were considered and deferred.

## Anti-patterns to avoid

- **Don't merge STYLE into IDENTITY.** They are different jobs.
  IDENTITY says who the agent is (career, history, relationships,
  decision-style). STYLE says how the agent speaks (sentence shape,
  forbidden openers, emoji budget, anti-mirror). Mixing them is the
  current state — and is a mechanism behind the drift.
- **Don't put style rules anywhere but at the top of the persona
  chain.** Burying voice rules below 200 lines of operational policy is
  why they don't land.
- **Don't make voice rules vibes.** "Be playful and a little savage"
  is a description. "Cap 18 words. Never start with 'Of course'.
  Emoji budget 2-4." is a contract. The framework should accept
  contracts, not vibes.
- **Don't mirror by default.** The default LLM behavior is mirror;
  the framework's job is to push back. Anti-mirror is a named clause,
  not an implication.
- **Don't override an agent's own RULES from the framework.** The
  caveman cross-contamination bug (`render_preamble` injecting caveman
  when STYLE says no) is the canonical example. Framework defaults
  are defaults; agent rules win.
- **Don't reach for fine-tuning before structural prompt fixes are
  measured.** Tier 3 is the lazy answer. Tier 1 is cheap, structural,
  and probably enough.
- **Don't hard-code agent archetypes in framework code.** Archetypes
  live in `templates/style/` as starter files, copied into the
  instance at setup time. The framework reads `STYLE.md`, not a
  `style_archetype: gen-z-social` config field. Operators can edit
  freely after setup; framework does not lock them in.

## Open questions for review

1. **Single anchor sentence vs. multi-line block?** Spec proposes a
   single ≤300-char summary line injected as `[Voice: ...]`. Alternative:
   a 3-line summary (voice / forbidden openers / register).
   Tradeoff: more tokens per turn for stronger anchoring. Default to
   single line, revisit if drift remains.

2. **Voice anchor in `[Voice: ...]` brackets vs. inline preamble vs.
   markdown headed section?** Spec proposes brackets — model treats
   bracketed text as out-of-band, less likely to echo. Alternative is
   `## Voice anchor\n<line>\n\n## Message\n<body>` which is more
   structured but more verbose. Default to brackets.

3. **`pin_to_default_brain` granularity.** Currently global per
   instance. Per-conversation or per-channel could be useful (e.g.
   pin in private DMs, allow triage in group chats). Out of scope for
   v1.

4. **Migration urgency.** Existing instances are running today
   without STYLE.md. Should the framework log a `jc doctor` warning
   from day one, or wait until v2 once the archetype starter files
   are mature?

## Out-of-scope (explicit)

- The contents of any specific instance's `STYLE.md`. That is operator
  authoring work, not framework work. The spec defines the contract;
  agents fill it in.
- Multi-language tone rules (e.g. "use formal Lei in Italian, casual
  tu in English"). Possible follow-up; STYLE.md format does not
  preclude it but does not specify it either.
- Voice (audio) output tone — TTS provider settings live in
  `channels.voice` config and are unrelated to text-tone anchoring.
