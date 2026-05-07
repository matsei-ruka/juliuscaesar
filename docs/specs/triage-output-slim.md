# Spec: Slim triage output — class is enough

**Status:** Implemented
**Date:** 2026-05-07
**Branch base:** `main`
**Owner:** tbd

## Goal

Stop asking the triage classifier for fields the gateway does not act on.

Today the classifier's required JSON schema is:

```json
{"class":"<class>","brain":"<brain>","confidence":<0..1>}
```

…but `brain` is ignored whenever the operator has set `triage_routing.<class>`,
which is the normal pattern. Tighten the contract so triage owns the
*decision* (class + confidence) and the operator owns the *mapping* (class →
brain:model) entirely in config.

This:

- Removes wasted tokens / latency on every triage call.
- Removes coupling between the classifier prompt and the brain catalog
  (which rotates).
- Removes a foot-gun where the classifier picks brains the operator has
  not sanctioned.
- Makes routing 100% predictable from yaml — no "what did the classifier
  pick today?".

## Non-goals

- Not changing how triage is invoked (cache, threshold, sticky, fallback all
  unchanged).
- Not changing the routing decision tree in `lib/gateway/router.py` — the
  router still consumes `TriageHint(brain, model, confidence)`. The
  brain/model on `TriageHint` is just sourced 100% from config now instead
  of half from the classifier.
- Not removing the `unsafe` class. `is_unsafe()` stays.
- Not breaking instances that rely on the classifier's `brain` field today
  (no `triage_routing` set). Back-compat shim for one release.
- Not changing `metrics.py` schema. The metrics DB still records `brain`,
  sourced from the routed value (post-config-mapping), not the classifier.

## Current behavior

### `TriageResult` fields and actual usage

`lib/gateway/triage/base.py:28-37`:

```python
@dataclass(frozen=True)
class TriageResult:
    class_: str
    brain: str
    confidence: float
    reasoning: str | None = None
    raw: str | None = None
```

| Field | Source | Consumer | Verdict |
|-------|--------|----------|---------|
| `class_` | classifier JSON | `_triage_to_hint` lookup key, metrics, `is_unsafe()` | **load-bearing** |
| `confidence` | classifier JSON | threshold gate (`router.py:102`) | **load-bearing** |
| `brain` | classifier JSON | fallback when `triage_routing.<class>` is unset (`runtime.py:449`); logged; metrics | **mostly redundant** — overridden by config in normal use |
| `reasoning` | classifier JSON (never present — schema doesn't ask for it) | logged at `runtime.py:424` (always empty) | **dead** |
| `raw` | set internally to the JSON blob extracted from the API response (`base.py:74`) | logged at `runtime.py:423`; debug aid in `_failure(...)` returns | keep — debug only, never claimed from classifier |

### Mapping flow today

`lib/gateway/runtime.py:446-451`:

```python
def _triage_to_hint(self, result: TriageResult) -> router.TriageHint:
    spec = self.config.triage.routing.get(result.class_, result.brain)
    brain, _, model = spec.partition(":")
    return router.TriageHint(brain=brain or result.brain, model=model or None, confidence=result.confidence)
```

If `triage_routing[class_]` exists → use it (classifier `brain` discarded).
Otherwise → fall back to classifier `brain`.

### Prompt today

`lib/gateway/triage/prompt.md:1-15` (excerpt):

```
You are a triage classifier. You output exactly one JSON object on a single line.

Schema: {"class":"<class>","brain":"<brain>","confidence":<0..1>}

Classes and their default brains:
- smalltalk     → claude:haiku-4-5
- quick         → claude:sonnet-4-6
- analysis      → claude:opus-4-7-1m
...

Pick the class. Then pick the brain — usually the default for that class, but you may override if the message clearly demands more or less power.
```

The prompt teaches the classifier the brain catalog. Catalog rotates more
often than this spec does; the operator's yaml is the right place for it.

## Desired behavior

### New classifier contract

`prompt.md` v2 — class + confidence, nothing else:

```
Schema: {"class":"<class>","confidence":<0..1>}

Classes:
- smalltalk     (greetings, banter, quick chitchat)
- quick         (single-step questions, < 1 min work)
- analysis      (research, comparison, multi-step reasoning)
- code          (build, edit, refactor, debug)
- image         (multimodal, image gen, image read)
- voice         (transcribed voice; re-triage on text)
- system        (worker events, watchdog alerts, scheduled tasks)
- unsafe        (out-of-policy; do not invoke a brain)

Pick the class. Output exactly one JSON object on a single line.
```

No brain names in the classifier prompt. Less context, fewer tokens, no
coupling to brain catalog.

### `TriageResult` v2

```python
@dataclass(frozen=True)
class TriageResult:
    class_: str
    confidence: float
    raw: str | None = None
    # `brain` and `reasoning` removed. See "Back-compat" for parsing.
```

`is_unsafe()` unchanged.

### `_triage_to_hint()` v2

`triage_routing` becomes the sole `class → brain:model` map. When a class
is unmapped, fall back to `default_fallback_brain` (which already exists
on `TriageConfig`):

```python
def _triage_to_hint(self, result: TriageResult) -> router.TriageHint | None:
    spec = self.config.triage.routing.get(result.class_) \
        or self.config.triage.fallback_brain
    if not spec:
        return None  # no mapping; let router fall through to channel default
    brain, _, model = spec.partition(":")
    return router.TriageHint(brain=brain, model=(model or None), confidence=result.confidence)
```

### `parse_triage_json()` v2

`lib/gateway/triage/base.py:54-82` updated:

- `class_` and `confidence` are required.
- `brain` field is **accepted but ignored** for one release (back-compat with
  classifiers that haven't been re-prompted yet, including any third-party
  triage backends operators may have wired).
- `reasoning` is dropped from parsing entirely.
- Validation of `class_` against `CLASSES` unchanged.

```python
def parse_triage_json(raw: str) -> TriageResult | None:
    ...
    if not cls:
        return None
    return TriageResult(
        class_=cls if cls in CLASSES else "quick",
        confidence=max(0.0, min(1.0, confidence)),
        raw=blob,
    )
```

### Default `triage_routing`

To avoid surprise behavior changes for instances that did not set
`triage_routing` and were relying on the classifier's `brain`, ship a
default `triage_routing` matching the historical `prompt.md` defaults:

```python
DEFAULT_TRIAGE_ROUTING = {
    "smalltalk": "claude:haiku-4-5",
    "quick":     "claude:sonnet-4-6",
    "analysis":  "claude:opus-4-7-1m",
    "code":      "claude:sonnet-4-6",
    "image":     "claude:sonnet-4-6",
    "voice":     "claude:sonnet-4-6",
    "system":    "claude:haiku-4-5",
}
```

Loaded as the default in `_load_triage()`. Operator-provided
`triage_routing.<class>: <spec>` values overlay this map per class, so a
single override does not erase the rest of the historical defaults.

### `_failure()` returns

Every backend's `_failure()` builder
(`openrouter.py`, `ollama.py`, `claude_channel.py`, `codex_api.py`,
`factory._NoneBackend`) currently constructs a `TriageResult` with
`brain="claude:sonnet-4-6"` and `reasoning="..."`. After this change those
fields are gone. The fallback semantics move:

- Confidence stays `0.0` so the router triggers the existing
  `triage_fallback` branch and uses `default_fallback_brain` from config.
- Failure reason text is preserved in `raw`, which the gateway log already
  previews. It was effectively log-only anyway.

Net behavior: fallback path is unchanged; the operator's
`default_fallback_brain` is honored consistently instead of each backend
hardcoding `claude:sonnet-4-6`.

### `metrics.record()`

`lib/gateway/triage/metrics.py:37` currently writes `result.brain`. After
this change, `brain` is no longer on `TriageResult`. Update the recorder to
take the routed brain from `_triage_to_hint()`'s output (or pass it as a
parameter) so the metrics DB still records the brain that was actually used,
not the classifier's discarded suggestion. Schema unchanged.

Call site in `runtime.py:434`:

```python
hint = self._triage_to_hint(result)
self.metrics.record(result, brain=routed_or_fallback_brain, fallback=below)
```

## Code plan

Files to modify:

- `lib/gateway/triage/base.py`
  - Drop `brain` and `reasoning` from `TriageResult`.
  - Update `parse_triage_json()` to ignore them (accept-but-discard for
    one release).
  - Update default prompt fallback (`_DEFAULT_PROMPT`) to v2 schema.
- `lib/gateway/triage/prompt.md`
  - Rewrite to v2 schema (class + confidence only).
- `lib/gateway/triage/{openrouter,ollama,claude_channel,codex_api}.py`
  - Update `_failure(...)` to drop `brain` / `reasoning`. Move reason text
    into log statements.
- `lib/gateway/triage/factory.py`
  - `_NoneBackend.classify()` returns the new shape.
- `lib/gateway/triage/metrics.py`
  - `record()` accepts the routed `brain` as a parameter.
- `lib/gateway/runtime.py`
  - `_triage_to_hint()` per "Desired behavior".
  - `_maybe_triage()` log line: drop `brain={result.brain}`; log routed
    brain instead (or drop entirely — class is enough for triage logs).
  - `metrics.record(result, brain=…, fallback=…)` call updated.
- `lib/gateway/config.py`
  - `_load_triage()`: ship `DEFAULT_TRIAGE_ROUTING` as the default for
    `routing` when yaml omits it. Operator-set values override per-class.

Files to add:

- `tests/gateway/test_triage_slim.py` (or extend `test_triage.py`):
  - Classifier returning v2 JSON (`class + confidence` only) → parsed.
  - Classifier returning v1 JSON (with extra `brain`) → parsed,
    `brain` silently discarded.
  - `triage_routing.<class>` set → routed to that brain.
  - `triage_routing.<class>` unset → falls back to `default_fallback_brain`.
  - `unsafe` class → still rejected.

## Back-compat

- Classifiers that still emit the v1 schema (with `brain`) keep working —
  the parser reads `class` and `confidence`, ignores everything else.
- Existing instances that **set** `triage_routing` keep those class-specific
  mappings; omitted classes now receive the historical default mapping.
- Existing instances that **did not set** `triage_routing` and relied on
  the classifier's `brain` get the new `DEFAULT_TRIAGE_ROUTING` as a
  default, which mirrors the historical `prompt.md` defaults. Net behavior
  for normal traffic: identical.
- Edge case: an operator who customized `prompt.md` so the classifier picked
  unusual brains will lose that override. A doctor warning for that rare case
  remains phase 2.

## Test plan

- `parse_triage_json` accepts `{"class":"quick","confidence":0.9}`.
- `parse_triage_json` accepts `{"class":"quick","brain":"x","confidence":0.9}` —
  ignores `brain`.
- `parse_triage_json` rejects missing `class`.
- `_triage_to_hint`: `routing.code` set to `claude:sonnet-4-6` →
  `TriageHint(brain="claude", model="sonnet-4-6", …)`.
- `_triage_to_hint`: class unmapped, `fallback_brain=claude:sonnet-4-6` →
  `TriageHint(brain="claude", model="sonnet-4-6", …)`.
- `_triage_to_hint`: class unmapped, `fallback_brain=""` → `None`,
  `router.route()` uses channel default.
- `_NoneBackend` returns `class_="quick"`, no `brain` attribute.
- Metrics DB row written contains the routed brain (post-mapping), not
  classifier output.
- `unsafe` class still rejects.

## Rollout plan

**Phase 1 — Land slim contract.** Ship the parser change, the new
`_triage_to_hint`, the default `triage_routing` map, and updated
backends/metrics. `prompt.md` rewritten. Tests green. No operator action
required for normal cases.

**Phase 2 — Doctor surface.** `jc doctor` checks: triage backend
configured? `triage_routing` mapped for all classes? `default_fallback_brain`
set? Warn on gaps.

**Phase 3 (separate spec, optional).** Once adopted, drop the v1 parser
back-compat (require `class + confidence` only). Lowest priority — the
back-compat is essentially free.

## Open questions

1. Should `confidence` be optional too? A classifier that only outputs
   `class` could default to `confidence=1.0` and bypass the threshold.
   Tempting (even simpler prompt) but dangerous: removes the operator's
   "below threshold → fallback" lever. **Recommend: keep `confidence`
   required.**
2. Should `triage_routing` be mandatory at config-validation time once
   triage is enabled? Today it's optional. Recommend a doctor warning,
   not a hard error, to avoid breaking existing instances on upgrade.
3. Should the default `triage_routing` ship in the instance template
   (`templates/init-instance/ops/gateway.yaml`) instead of as a Python
   default? Visible in yaml is more debuggable but creates a migration
   cliff. Recommend: Python default + a comment block in the template
   showing the default for operators to copy when they want to override.
4. Should we emit a one-time deprecation log when a classifier returns
   the v1 `brain` field? Useful for catching stragglers; noisy on every
   call. Recommend: log once per backend instance, not per classify().
5. Anything that *should* live in classifier output but doesn't today?
   E.g. a confidence-per-class distribution. Out of scope; defer.

## Definition of done

- [x] `TriageResult` carries only `class_`, `confidence`, `raw`.
- [x] `prompt.md` rewritten to v2 schema (no brain catalog).
- [x] `parse_triage_json()` accepts v1 and v2 input identically (ignoring
      v1 extras).
- [x] `_triage_to_hint()` sources brain/model exclusively from
      `triage_routing` + `default_fallback_brain`.
- [x] `DEFAULT_TRIAGE_ROUTING` ships as the loader default.
- [x] All four real backends + `_NoneBackend` return v2 shape.
- [x] `metrics.record()` records the routed/fallback brain, not classifier's.
- [x] Targeted tests green:

```bash
pytest \
  tests/gateway/test_triage.py \
  tests/gateway/test_triage_slim.py \
  tests/gateway/test_router.py
```

- [x] No behavior change for instances with `triage_routing` set (verified
      by snapshot test on `_triage_to_hint`).

## Discrepancies with prompt

- The user characterized the classifier's `brain` as "always overridden by
  the configuration." Strictly: it is overridden only when
  `triage_routing.<class>` has an entry. The default config historically
  had no `triage_routing` map shipped, so operators who never set one were
  using the classifier's `brain`. This spec preserves their behavior by
  shipping `DEFAULT_TRIAGE_ROUTING` matching the historical `prompt.md`
  defaults — closing the gap and making "always config-driven" actually
  true.
- The prompt asked to "remove what we don't use." `reasoning` is removed
  outright (dead). `brain` is removed from the contract but accepted-and-
  ignored in the parser for one release for back-compat. `raw` is kept —
  it is set internally for debug logs and never claimed from the classifier.
