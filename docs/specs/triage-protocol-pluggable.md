# Spec: Pluggable triage protocol (OpenAI-compatible + Anthropic)

**Status:** Implemented
**Date:** 2026-05-07
**Branch base:** `main`
**Owner:** tbd

## Goal

Replace the per-provider triage backend list (`ollama`, `openrouter`,
`claude-channel`, `codex_api`) with a **single generic, protocol-aware HTTP
classifier**. The same backend targets any provider that speaks one of a small
set of wire shapes; today: OpenAI-compatible chat completions, and Anthropic
`/v1/messages`.

The motivating use case: stop paying OpenRouter's margin and call DeepSeek,
Groq, Together, Fireworks, Cerebras, Anthropic, etc. directly. Most speak the
OpenAI-compatible protocol; Anthropic has its own. A `protocol` config field
selects the wire shape; the backend stays generic.

## Non-goals

- Not adding the classifier as an *answer brain*. Triage scope only.
- Not building a generic answer-brain HTTP adapter. That is a separate spec.
- Not adding streaming. Triage is one-shot, low-token, blocking.
- Not breaking the existing `triage: openrouter` config. Existing instances must
  keep working unchanged.
- Not deprecating `ollama`, `claude-channel`, or `codex_api` in this spec. They
  stay; they are not HTTP-protocol-shaped (Ollama has its own API; the Codex
  Responses path uses subscription auth; Claude-channel uses a screen-attached
  CLI). Future converging is out of scope.
- Not changing the slim triage parser/prompt contract from
  `triage-output-slim`: classifiers still produce `class + confidence`.

## Current behavior

### Backend selection

`lib/gateway/triage/factory.py:build_backend()` switches on `cfg.backend`:

```python
if backend in ("none", "always", ""):  return _NoneBackend()
if backend == "ollama":                 return OllamaTriage(cfg)
if backend == "openrouter":             return OpenRouterTriage(cfg, instance_dir)
if backend == "claude-channel":         return ClaudeChannelTriage(cfg)
if backend in ("codex_api", "codex-api"): return CodexApiTriage(cfg, instance_dir, ...)
raise ValueError(f"unknown triage backend: {backend}")
```

`SUPPORTED_TRIAGE_BACKENDS` in `lib/gateway/config.py:17`:

```python
SUPPORTED_TRIAGE_BACKENDS = (
    "none",
    "always",
    "ollama",
    "openrouter",
    "claude-channel",
    "codex_api",
)
```

### OpenRouter call shape

`lib/gateway/triage/openrouter.py` posts to a hardcoded URL:

```python
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
```

Request body:

```json
{
  "model": "<cfg.openrouter_model>",
  "messages": [
    {"role": "system", "content": "Output exactly one JSON object on one line."},
    {"role": "user",   "content": "<rendered prompt>"}
  ],
  "temperature": 0.0
}
```

Headers: `Authorization: Bearer <key>`, plus OpenRouter-specific
`HTTP-Referer` and `X-Title`. Response: standard OpenAI-compatible
`{choices:[{message:{content}}]}`. `parse_triage_json()` extracts the JSON line.
On any HTTP / parse failure the backend returns a low-confidence "quick" result
via `_failure(...)` so the router falls back gracefully (see
`lib/gateway/triage/openrouter.py:75-82`).

### `TriageConfig` fields

`lib/gateway/config.py:51-67`:

```python
@dataclass(frozen=True)
class TriageConfig:
    backend: str = "none"
    confidence_threshold: float = 0.7
    fallback_brain: str = "claude:sonnet-4-6"
    cache_ttl_seconds: int = 30
    sticky_idle_seconds: int = 0
    routing: dict[str, str] = field(default_factory=dict)
    ollama_model: str = "phi3:mini"
    ollama_host: str = "http://localhost:11434"
    ollama_timeout_seconds: int = 5
    openrouter_model: str = "meta-llama/llama-3.1-8b-instruct"
    openrouter_api_key_env: str = "OPENROUTER_API_KEY"
    openrouter_timeout_seconds: int = 5
    claude_triage_screen: str = "jc-triage"
    claude_triage_model: str = "claude-haiku-4-5"
    claude_triage_port: int = 9876
```

API key reads go through `env_value(instance_dir, name)` at
`lib/gateway/config.py:205-206`, which prefers process env then the instance
`.env`. Validation lives in `_validate_raw_config()` at
`lib/gateway/config.py:322`, with the triage-specific block at lines `388-416`.
The loader is `_load_triage()` at lines `676-713`.

## Desired behavior

### New backend: `api_classifier`

A generic HTTP classifier that takes:

- `protocol` — selects the wire shape (initially `openai_compat` or
  `anthropic`).
- `base_url` — provider root (e.g. `https://api.deepseek.com/v1`).
- `api_key_env` — env var name to read the bearer/key from.
- `model` — model id passed to the provider.
- `timeout_seconds`, `max_tokens` — per-call limits.

Internally the backend delegates encode/decode to a small protocol module:

```
lib/gateway/triage/api_classifier.py
lib/gateway/triage/protocols/__init__.py
lib/gateway/triage/protocols/base.py            # Protocol ABC
lib/gateway/triage/protocols/openai_compat.py
lib/gateway/triage/protocols/anthropic.py
```

`Protocol` ABC:

```python
class Protocol:
    name: str
    def url(self, base_url: str) -> str: ...
    def headers(self, api_key: str) -> dict[str, str]: ...
    def encode(self, prompt: str, *, model: str, max_tokens: int | None) -> dict: ...
    def decode(self, payload: dict) -> str: ...        # extract assistant text
    def validate_config(self, cfg: TriageConfig) -> list[str]: ...
```

The backend itself does the HTTP work, error handling, and returns the same
`TriageResult` failure shape as the existing backends. `parse_triage_json()` is
unchanged.

### Config schema (new fields)

```yaml
triage: api_classifier
triage_protocol: openai_compat        # or: anthropic
triage_base_url: https://api.deepseek.com/v1
triage_api_key_env: DEEPSEEK_API_KEY
triage_model: deepseek-chat
triage_timeout_seconds: 5
triage_max_tokens: 200                # required when protocol=anthropic
```

Anthropic example:

```yaml
triage: api_classifier
triage_protocol: anthropic
triage_base_url: https://api.anthropic.com/v1
triage_api_key_env: ANTHROPIC_API_KEY
triage_model: claude-haiku-4-5
triage_timeout_seconds: 5
triage_max_tokens: 200
```

Groq / Together / Fireworks / Cerebras: same as DeepSeek, only `triage_base_url`,
`triage_api_key_env`, and `triage_model` change.

### Wire shapes (side-by-side)

#### `openai_compat`

- **URL:** `POST <base_url>/chat/completions`
- **Headers:**
  - `Authorization: Bearer <api_key>`
  - `Content-Type: application/json`
- **Body:**
  ```json
  {
    "model": "<model>",
    "messages": [
      {"role": "system", "content": "Output exactly one JSON object on one line."},
      {"role": "user",   "content": "<rendered prompt>"}
    ],
    "temperature": 0.0,
    "max_tokens": <triage_max_tokens or omitted>
  }
  ```
- **Response (relevant fields):**
  ```json
  {
    "choices": [{"message": {"content": "<text>"}}],
    "usage":   {"prompt_tokens": …, "completion_tokens": …}
  }
  ```
- **Decode:** `payload["choices"][0]["message"]["content"]`.

#### `anthropic`

- **URL:** `POST <base_url>/messages` (typical `<base_url>` is
  `https://api.anthropic.com/v1`).
- **Headers:**
  - `x-api-key: <api_key>`
  - `anthropic-version: 2023-06-01`
  - `content-type: application/json`
- **Body:**
  ```json
  {
    "model": "<model>",
    "max_tokens": <triage_max_tokens, REQUIRED>,
    "system": "Output exactly one JSON object on one line.",
    "messages": [
      {"role": "user", "content": "<rendered prompt>"}
    ],
    "temperature": 0.0
  }
  ```
- **Response (relevant fields):**
  ```json
  {
    "content":     [{"type": "text", "text": "<text>"}],
    "stop_reason": "end_turn" | "max_tokens" | …,
    "usage":       {"input_tokens": …, "output_tokens": …}
  }
  ```
- **Decode:** concatenate `text` fields where `type == "text"`. If
  `stop_reason == "max_tokens"` and the decoded text fails JSON parse, surface
  it as a parse failure with the same `_failure` shape used elsewhere.

### Validation rules

Added in `_validate_raw_config()`:

- `triage_protocol` must be one of `{openai_compat, anthropic}` when
  `triage: api_classifier`. Unknown protocols are config errors.
- `triage_protocol` outside `api_classifier` is a config error (avoid
  silent-ignore drift).
- `triage_base_url` must be set and start with `http://` or `https://`.
- `triage_api_key_env` must be a non-empty string. The actual key being absent
  at runtime degrades gracefully (matching today's `openrouter` behavior).
- `triage_timeout_seconds` if set must be a positive number ≤ 60.
- `triage_max_tokens` if set must be a positive int ≤ 4096.
- `protocol == anthropic` **requires** `triage_max_tokens`. The Anthropic API
  rejects `/messages` without it; failing fast at config time beats failing on
  the first inbound message.

### Back-compat

`triage: openrouter` keeps working with no config changes. Implementation
becomes a thin shim:

```python
# lib/gateway/triage/openrouter.py
class OpenRouterTriage(ApiClassifierTriage):
    name = "openrouter"
    def __init__(self, cfg, instance_dir):
        super().__init__(
            cfg,
            instance_dir,
            protocol_name="openai_compat",
            base_url="https://openrouter.ai/api/v1",
            api_key_env=cfg.openrouter_api_key_env,
            model=cfg.openrouter_model,
            timeout_seconds=cfg.openrouter_timeout_seconds,
            extra_headers={
                "HTTP-Referer": "https://github.com/matsei-ruka/juliuscaesar",
                "X-Title": "JuliusCaesar Gateway",
            },
        )
```

This keeps the OpenRouter-specific `HTTP-Referer` / `X-Title` headers without
forcing every `api_classifier` user to think about them. A `triage_extra_headers`
yaml field is left as an open question (see below).

`SUPPORTED_TRIAGE_BACKENDS` gains `api_classifier`. Nothing is removed.

### Extension point for future protocols

To add a third protocol (e.g. `gemini`):

1. New file `lib/gateway/triage/protocols/<name>.py` implementing the `Protocol`
   ABC.
2. Register it in `protocols/__init__.py:PROTOCOLS`.
3. Extend the `triage_protocol` enum in `_validate_raw_config()`.
4. Add tests under `tests/gateway/triage/test_protocol_<name>.py`.

No changes to `api_classifier.py` itself. That is the point.

## Code plan

Files to add:

- `lib/gateway/triage/api_classifier.py` — generic backend; owns HTTP, timeout,
  error mapping, and `_failure` returns. Pluggable encoder/decoder via
  `Protocol`.
- `lib/gateway/triage/protocols/__init__.py` — `PROTOCOLS` registry +
  `get_protocol(name) -> Protocol`.
- `lib/gateway/triage/protocols/base.py` — `Protocol` ABC.
- `lib/gateway/triage/protocols/openai_compat.py` — encode/decode for
  `chat/completions`.
- `lib/gateway/triage/protocols/anthropic.py` — encode/decode for `/v1/messages`.

Files to modify:

- `lib/gateway/triage/factory.py` — add the `api_classifier` branch; keep all
  other branches.
- `lib/gateway/triage/openrouter.py` — refactor to a thin
  `ApiClassifierTriage` subclass with the OpenRouter URL/headers preset
  (preserves `name = "openrouter"` for metrics/logs).
- `lib/gateway/config.py`:
  - Add `api_classifier` to `SUPPORTED_TRIAGE_BACKENDS`.
  - Extend `TriageConfig` with `protocol`, `base_url`, `api_key_env`, `model`,
    `timeout_seconds`, `max_tokens`. Field names listed in the schema block
    above. Defaults: `protocol="openai_compat"`, others empty/None.
  - Add fields to `allowed_top` and to the in-`triage:` block allow-list at
    `_validate_raw_config()`.
  - Validation rules from the "Validation rules" section.
  - Loader updates in `_load_triage()`.
- `tests/gateway/test_triage.py` — add cases (or split into a new
  `tests/gateway/triage/` directory if it grows; the prompt suggests the latter).

Tests to add (location: `tests/gateway/triage/` if split, else appended to
`tests/gateway/test_triage.py`):

- `test_api_classifier_openai_compat.py` — DeepSeek-shaped 200 response →
  parsed correctly.
- `test_api_classifier_anthropic.py` — Anthropic-shaped 200 response → parsed
  correctly.
- `test_api_classifier_failure.py` — missing key, 401, 5xx, timeout, malformed
  JSON, `stop_reason=max_tokens` truncation.
- `test_api_classifier_back_compat.py` — `triage: openrouter` config produces
  the same outbound request shape and headers as before (snapshot/golden
  comparison against a recorded fixture).
- `test_config_triage_validation.py` — bad combos rejected (bad
  `triage_protocol`, missing `base_url`, missing `triage_max_tokens` with
  `protocol=anthropic`, etc.).

Mocking: use the same `urllib.request.urlopen` patching pattern already used in
`tests/gateway/test_triage.py` (no new HTTP framework introduced).

## Test plan (acceptance-level)

Required passing cases:

1. `triage: api_classifier`, `protocol: openai_compat`, mocked DeepSeek 200
   response with a valid JSON line in `choices[0].message.content` →
   `TriageResult(class_=..., confidence>0)`.
2. `triage: api_classifier`, `protocol: anthropic`, mocked Anthropic 200 with
   `content=[{type:"text", text:"<json>"}]` → same slim parsed result.
3. `triage: api_classifier`, missing API key env → `_failure("missing
   <KEY_ENV>")` with `class_="quick"` and confidence `0.0`. Identical fallback
   shape to today's openrouter backend.
4. `triage: api_classifier`, mocked 401 → graceful `_failure(...)`, no
   exception escapes.
5. `triage: api_classifier`, mocked 500 → graceful `_failure(...)`.
6. `triage: api_classifier`, simulated `URLError` / `TimeoutError` →
   graceful `_failure(...)`.
7. `triage: api_classifier`, `protocol: anthropic`, response with
   `stop_reason="max_tokens"` and unparseable text → `_failure("anthropic
   truncated …")` with `raw=` set to the captured snippet.
8. `triage: openrouter` snapshot test: outbound HTTP request body and headers
   are byte-identical to the pre-refactor implementation (covers
   `HTTP-Referer`, `X-Title`, `Authorization`, body shape).
9. Config validation:
   - `triage: api_classifier` without `triage_base_url` → error.
   - `triage: api_classifier`, `triage_protocol: anthropic` without
     `triage_max_tokens` → error.
   - `triage_protocol: bogus` → error.
   - `triage_protocol: gemini` (not yet added) → error, with a clear message
     listing supported values.

## Rollout plan & migration

**Phase 1 — Land generic backend.** Ship `api_classifier`, the two protocol
modules, validation, and tests. `openrouter` becomes a shim. No template or
default changes; existing instances are unaffected.

**Phase 2 — Update `jc setup` defaults.** Update `jc setup` triage prompts to
offer `api_classifier` with a provider picker (DeepSeek / Anthropic / Groq /
OpenRouter-compat / custom). Default for new instances stays `triage: none`.

**Phase 3 (optional, separate spec).** Once adoption is verified, deprecate
the `triage: openrouter` alias by emitting a doctor warning and a
configuration-migration hint, but keep it functional. Removal is a major
version change and out of scope here.

## Open questions

1. Should `triage_max_tokens` default to a low value (e.g. `64` or `128`) for
   `openai_compat` since the classifier output is a single JSON line? Lower =
   cheaper and faster; risk is provider-specific tokenizer surprises.
2. Should we add a `triage_extra_headers: {key: value}` map so users can attach
   custom headers (Cloudflare worker auth, organization IDs like
   `OpenAI-Organization`, Anthropic beta flags)? Convenient but a new injection
   surface — header values must be string-typed and not reference env
   indirection without an explicit `${ENV:NAME}` syntax.
3. Should `protocol` accept `gemini` (Google's native shape) in this PR or be
   pushed to a separate spec? Gemini's schema differs more than Anthropic's
   (`contents`/`parts`, no system role) and would benefit from its own design
   pass.
4. Should the shim `openrouter` backend still send `HTTP-Referer` and
   `X-Title`? They are requested by OpenRouter docs but not required;
   simplifying the shim slightly changes outbound traffic for existing
   instances.
5. Should `triage_api_key_env` allow a raw key as a fallback (e.g. accept
   `triage_api_key: "sk-..."` for users without `.env`)? Risk: keys end up in
   yaml, then in git. Probably no.

## Definition of done

- [x] `lib/gateway/triage/api_classifier.py` implemented with both protocol
      modules under `protocols/`.
- [x] `factory.build_backend()` routes `api_classifier` to the new backend.
- [x] `OpenRouterTriage` is a thin shim over `ApiClassifierTriage`; existing
      `triage: openrouter` config produces the same outbound request as before
      (verified by snapshot test).
- [x] `TriageConfig` carries the new fields; loader and validator updated.
- [x] Validation rejects: unsupported `triage_protocol`, missing
      `triage_base_url`, missing `triage_max_tokens` when
      `protocol=anthropic`, non-`http(s)` URLs.
- [x] `parse_triage_json()` and `render_prompt()` are unchanged by this PR;
      `TriageResult` remains the slim #38 shape.
- [x] All failure paths (missing key, 401, 5xx, timeout, malformed JSON,
      Anthropic `max_tokens` truncation) return the existing `_failure(...)`
      shape — no exceptions escape.
- [x] `jc doctor` reports the configured triage backend, protocol, base URL
      (host only), model, and whether the api-key env var resolves.
- [x] Targeted tests green:

```bash
pytest \
  tests/gateway/test_triage.py \
  tests/gateway/triage/
```

- [x] `docs/MIGRATION-*.md` updated with the direct-provider switch guide
      (Phase 2).

## Discrepancies with prompt

- Prompt suggested `tests/gateway/triage/` as a new test directory. The repo
  currently uses a flat `tests/gateway/test_triage.py`. The spec keeps both
  options open: the implementer can append cases to the existing file or
  introduce the subdirectory if size warrants it. No decision is forced here.
- Prompt referenced `lib/gateway/triage/{factory.py,openrouter.py,codex_api.py}`
  as the anchors. All three exist as named. The current OpenRouter backend
  hardcodes `OPENROUTER_URL` rather than deriving it from
  `cfg.openrouter_base_url` (no such field exists today). The shim approach in
  this spec preserves that hardcoded constant inside the shim — no new
  `openrouter_base_url` field is required.
- Prompt's example Anthropic config shows
  `triage_base_url: https://api.deepseek.com/v1` next to
  `triage_protocol: openai_compat`. That is the OpenAI-compatible example;
  the Anthropic example is given separately in this spec.
