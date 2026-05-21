# Smart slot classifier — embedding + entity pre-filter

**Status:** ready-to-implement (2026-05-21)
**Owner:** Luca + Rachel
**Touches:** `lib/gateway/runtime.py`
**Branch:** main (add to existing parallel-slots implementation)

## Problem

The LLM classifier (`_classify_slot_affinity`) returns `related:0` for all messages,
including obvious topic shifts like "what about bnesim?" and "meteo dubai?". Root cause:
dense conversation history biases the model toward continuity. Prompt tuning (`f8c3468`)
helped but did not fix it structurally.

## Goal

Replace the LLM call with a fast, deterministic **pre-filter** that catches obvious
topic changes without any network round-trip. LLM remains only as a fallback for
genuinely ambiguous signals.

## Non-goals

- Cross-slot context sharing.
- Full NLP pipeline (spaCy, transformers, heavy deps).
- Modifying slot session history or summary logic.

## Design

### Two-signal pre-filter

**Signal 1 — Token overlap (Jaccard)**

Tokenize new message and slot summary into lowercase content words (strip stopwords,
punctuation, numbers). Compute Jaccard similarity:

```
J(A, B) = |A ∩ B| / |A ∪ B|
```

- J < `low_threshold` (default 0.05) → almost certainly **unrelated** → return `None` immediately
- J ≥ `high_threshold` (default 0.35) → strong lexical match → LLM call (could be continuation)
- 0.05 ≤ J < 0.35 → proceed to Signal 2

**Signal 2 — Named-entity presence check**

Extract candidate named entities from the new message: tokens that are:
- Title-cased (`Bnesim`, `Dubai`, `Sophie`, `Daniel`)
- All-caps acronyms of ≥2 chars (`BNESIM`, `UAE`)
- Known slot-keyed entity types (numbers prefixed by `VM`, `ip`, `vm`)

If the new message contains ≥1 named entity NOT present in ANY slot summary → **unrelated** → return `None`.

If it contains ≥1 entity that IS present in a specific slot → candidate for that slot,
pass final verdict to LLM (with a tighter, entity-anchored prompt).

### Decision tree

```
new_message
    │
    ▼
Jaccard(msg, each slot summary)
    │
    ├── ALL slots: J < 0.05  ──→  return None  (unrelated, skip LLM)
    │
    ├── ONE slot: J ≥ 0.35   ──→  call LLM with that slot only
    │
    └── ambiguous (mid-range or multiple) ──→ entity check
            │
            ├── new entity not in any slot  ──→  return None (unrelated)
            │
            └── entity matches slot K  ──→  call LLM (reduced to slot K vs None)
```

### Stopwords (hardcoded, no dep)

```python
_STOPWORDS = frozenset({
    "a","an","the","is","in","on","at","to","for","of","and","or","but",
    "it","its","this","that","i","you","we","they","he","she","what","how",
    "do","did","can","could","will","would","please","ok","yes","no","not",
    "about","with","from","by","as","be","are","was","were","have","has",
    "had","so","then","when","where","which","who","also","just","still",
    "now","there","here","up","down","out","get","set","all","any","one",
    "more","my","your","our","their","me","him","her","us","them","let",
    "cosa","che","di","il","la","lo","le","gli","un","una","come","hai",
    "ho","ha","sei","si","sono","era","non","per","con","su","da",
})
```

### Configuration additions (gateway.yaml)

No new keys needed — the pre-filter is always active when `parallel` block is present.
Optional tuning via new `classifier` sub-keys:

```yaml
parallel:
  max_concurrent: 2
  classifier:
    backend: openrouter
    model: openrouter/deepseek/deepseek-chat
    timeout_seconds: 3
    cache_ttl_seconds: 30
    jaccard_low_threshold: 0.05    # new, optional, default 0.05
    jaccard_high_threshold: 0.35   # new, optional, default 0.35
```

If `backend: none` → pre-filter only, no LLM fallback (pure fast path).

### ParallelClassifierConfig changes

Add two optional float fields to the dataclass (defaulting to 0.05/0.35). Both read
from `gateway.yaml` via the existing Pydantic/dataclass config parser.

## Implementation plan

### Step 1 — Add `_prefilter_slot_affinity` to `GatewayRuntime`

New private method. Signature:

```python
def _prefilter_slot_affinity(
    self,
    message: str,
    slot_summaries: dict[int, str],
) -> tuple[str, int | None]:
    """Fast pre-filter before LLM classifier.

    Returns a tuple:
      ("unrelated", None)      — skip LLM, route to free slot
      ("related", slot_id)     — skip LLM, route to this slot
      ("ambiguous", None)      — call LLM
    """
```

### Step 2 — Wire pre-filter into `_classify_slot_affinity`

Before the existing LLM call:

```python
verdict, slot_id = self._prefilter_slot_affinity(event.content, slot_summaries)
if verdict == "unrelated":
    return None
if verdict == "related":
    return slot_id
# verdict == "ambiguous" → fall through to LLM
```

### Step 3 — Add module-level helpers (no class deps)

```python
def _tokenize(text: str) -> frozenset[str]: ...
def _jaccard(a: frozenset, b: frozenset) -> float: ...
def _extract_entities(text: str) -> frozenset[str]: ...
```

All pure Python, no imports beyond `re` and `string`.

### Step 4 — Tests

Add to `tests/gateway/test_parallel_slots.py` (or a new `test_slot_classifier.py`):

- `test_prefilter_obvious_unrelated` — "meteo dubai", slot 0 about Florian → unrelated
- `test_prefilter_obvious_unrelated_bnesim` — "what about bnesim", slot 0 about gateway restart → unrelated
- `test_prefilter_high_jaccard_falls_through` — follow-up on same topic → ambiguous
- `test_prefilter_entity_match_single_slot` — "restart Sophie" when slot 0 = Sophie thread → ambiguous (escalate to LLM)
- `test_prefilter_entity_new` — "restart Florian" when slot 0 = Sophie thread → unrelated
- `test_jaccard_edge_cases` — empty sets, identical sets, disjoint sets
- `test_tokenize_strips_stopwords` — verify stopword removal

### Step 5 — Commit + push

Commit to `main`. Push to origin. No gateway.yaml changes needed (config additions are optional/backward-compatible).

## Expected outcome

"meteo dubai?" when slot 0 is about gateway restarts:
- Jaccard("meteo dubai", slot0_summary) → ~0.0 → pre-filter returns `("unrelated", None)` → no LLM call → new slot

"what about bnesim?" when slot 0 is about Florian:
- Jaccard({"bnesim"}, {"florian","gateway","restart",...}) → 0.0 → unrelated immediately

"and also restart sophie" when slot 0 is about Sophie:
- Jaccard({"restart","sophie"}, {"sophie","gateway","pid",...}) → 0.2+ → entity check → "sophie" in slot 0 → ambiguous → LLM

## Risks

- Jaccard on very short messages (1-2 words) is noisy. Mitigated: when token set size < 2, skip Jaccard and go straight to entity check.
- False negatives (two unrelated topics using same vocabulary): LLM catches these since pre-filter passes through to LLM in ambiguous range.
- Stopword list is English+Italian only. Other languages fall through to LLM (correct behavior).

## No external dependencies

Implementation uses only Python stdlib (`re`, `string`). No pip installs required.
All thresholds are tunable via gateway.yaml. Can be disabled by setting `backend: none` (pre-filter only) or by not having a `parallel` block.
