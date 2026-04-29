# Codex auth extractor — direct OpenAI API access

**Status:** implemented (deviates from initial draft on the API target — see "Endpoint correction")
**Owner:** Rachel
**Target version:** 2026.04.29
**Branch:** `feat/codex-auth-extractor`

## Goal

Extract the bearer token from the local Codex CLI's auth state and use it to call the OpenAI Responses API directly — bypassing the Codex CLI for cases where we don't need its agent loop / shell tool / sandbox.

This unlocks:

1. **Triage** — fast, low-latency classification (which brain answers? which adapter is invoked?) using the cheapest GPT model available, billed against the same ChatGPT Plus subscription.
2. **Main chat / group chat** — direct API for inbound conversational events, keeping the Codex CLI only for *spawn* tasks (workers / coding loops). This is **opt-in per-instance**; default behavior stays unchanged.

The Codex CLI is unchanged and still owns the OAuth flow (login, refresh-on-launch). We are a *consumer* of its auth state, not a replacement for it.

## Non-goals

- We do not implement the OAuth login flow ourselves. `codex login` does that.
- We do not write to `auth.json` unless we successfully refreshed via OpenAI's token endpoint and need to persist the new tokens. (Avoid corrupting the file Codex CLI relies on.)
- We do not bundle or embed the Codex client ID — it stays a runtime-discovered value (extracted once at install time, see "Implementation").
- We do not handle API-key auth mode. This feature only supports `auth_mode: chatgpt` (subscription).

## Background — observed Codex auth model

The Codex CLI (`@openai/codex`) authenticates via OAuth against `https://auth.openai.com`. After `codex login`, it persists tokens to `~/.codex/auth.json`:

```json
{
  "auth_mode": "chatgpt",
  "OPENAI_API_KEY": null,
  "tokens": {
    "id_token":      "<JWT, OIDC ID token>",
    "access_token":  "<JWT, aud=https://api.openai.com/v1, lifetime ~10d>",
    "refresh_token": "rt_<opaque>",
    "account_id":    "<chatgpt account uuid>"
  },
  "last_refresh": "<ISO8601>"
}
```

The `access_token` JWT decodes to:

```json
{
  "aud": ["https://api.openai.com/v1"],
  "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
  "exp": <unix>,
  "iat": <unix>,
  "scp": ["openid", "profile", "email", "offline_access"],
  "https://api.openai.com/auth": {
    "chatgpt_plan_type": "plus" | "pro" | ...,
    "chatgpt_account_id": "...",
    "chatgpt_user_id": "...",
    "user_id": "..."
  }
}
```

**Key points:**

- The `access_token` is a valid `Authorization: Bearer` for `https://chatgpt.com/backend-api/codex/*` — **NOT** for `api.openai.com/v1/*` directly. The earlier draft assumed the latter; that endpoint rejects the subscription token with `401 Missing scopes: api.responses.write`. See "Endpoint correction" below.
- Lifetime is roughly 10 days. Refresh ahead of expiry to avoid mid-request 401s.
- Refresh endpoint: `POST https://auth.openai.com/oauth/token` with `Content-Type: application/x-www-form-urlencoded` and body:
  ```
  grant_type=refresh_token
  refresh_token=<refresh_token>
  client_id=<app_...>
  ```
- The response shape (inferred from Codex CLI behavior) is the standard OAuth token response: `{ access_token, id_token, refresh_token?, expires_in, token_type: "Bearer" }`. The refresh_token may rotate.
- Concurrency: Codex CLI may also refresh while we hold the file open. We must refresh under a file lock and re-read after acquiring it.
- Error codes Codex CLI handles (extracted from binary strings): `refresh_token_expired`, `refresh_token_already_used`, `refresh_token_revoked`. On any of these we log and surface "re-login required" to the operator; we do not retry.

The Codex client ID `app_EMoamEEZ73f0CkXaXp7hrann` was extracted from the released `@openai/codex-linux-x64` binary as a string constant. We treat it as a runtime parameter (configurable via env var override) rather than a hard-coded constant, so a future Codex update that rotates it doesn't silently break us.

## Endpoint correction (post-implementation finding)

During Phase 2 the assumption that the access token works against `api.openai.com/v1/responses` was disproved by a live call:

```
401 You have insufficient permissions for this operation.
    Missing scopes: api.responses.write.
```

The Codex CLI binary itself targets `https://chatgpt.com/backend-api/codex` (see strings in `@openai/codex-linux-x64`), and that endpoint *does* accept the subscription token. The Codex backend Responses API has a few constraints the public Responses API does not:

- `input` is a list of `{role, content}` objects. A bare string is rejected.
- `instructions` is required (any non-empty string is fine).
- `store: false` and `stream: true` are both required. Synchronous calls are rejected.
- `chatgpt-account-id` header is required (taken from the JWT `https://api.openai.com/auth.chatgpt_account_id` claim).
- The model catalog is the **Codex** catalog (`gpt-5.4-mini`, `gpt-5.4`, `gpt-5.5`, `gpt-5.3-codex`, …) — `gpt-4o-mini` and friends are rejected with `"<model>" is not supported when using Codex with a ChatGPT account`.
- `max_output_tokens` is rejected as `Unsupported parameter`. We accept the kwarg in the public API surface and silently drop it; callers control length via `instructions`.

`lib/codex_auth/responses.py` accumulates the SSE stream internally and presents a synchronous `ResponseResult` to callers, so the higher-level adapter and triage code stay simple. The launch model is `gpt-5.4-mini` (cheapest in the catalog).

## Architecture

New module: `lib/codex_auth/`

```
lib/codex_auth/
  __init__.py
  client.py        # OAuth refresh + bearer-token retrieval
  responses.py     # Thin OpenAI Responses API wrapper (used by triage + chat adapters)
  errors.py        # CodexAuthError, RefreshExpired, ReloginRequired
```

New CLI: `bin/jc-codex-auth` (Python shim) with subcommands:

- `status`          — show plan type, account id, time-to-expiry of access token, last refresh
- `refresh [--force]` — refresh now (skip if not expiring soon unless --force)
- `token`           — print a fresh bearer token to stdout (used by adapters / scripts)

New gateway/adapter integration (opt-in via instance config):

- `lib/gateway/triage/codex_api.py` — `CodexApiTriage` backend. Selected via `triage: codex_api` in `ops/gateway.yaml`. Uses `codex_auth.responses.complete()` against `gpt-5.4-mini` (overridable by repurposing `openrouter_model`).
- `lib/gateway/adapters/codex_api.py` + `lib/gateway/brains/codex_api.py` — main-chat adapter + Brain wrapper. Selectable per-channel via `channels.<name>.brain: codex_api`. The Brain overrides `invoke()` because there is no on-disk shell adapter to exec.

The existing `codex` CLI adapter stays untouched and continues to be the choice for `jc workers spawn --brain codex` (spawn / coding tasks need the CLI's tool loop).

## Auth flow (refresh logic)

```
def get_bearer():
    state = read_auth_json()                        # under shared lock
    token = state.tokens.access_token
    exp   = jwt_decode_unsafe(token).exp            # don't verify signature — we're not auth server
    skew  = 300                                     # 5min
    if now() + skew < exp:
        return token
    # Need refresh.
    with file_lock(~/.codex/auth.json.lock, exclusive, timeout=10s):
        state = read_auth_json()                    # re-read after lock
        token = state.tokens.access_token
        if now() + skew < jwt_decode_unsafe(token).exp:
            return token                            # someone else refreshed
        new = post_form(REFRESH_URL, {
            grant_type:    "refresh_token",
            refresh_token: state.tokens.refresh_token,
            client_id:     state.client_id_or_default,
        })
        # Translate known error codes to typed exceptions.
        if new.error == "invalid_grant":
            raise ReloginRequired(new.error_description)
        write_auth_json(state.with(
            tokens.access_token  = new.access_token,
            tokens.id_token      = new.id_token,
            tokens.refresh_token = new.refresh_token or state.tokens.refresh_token,
            last_refresh        = now_iso(),
        ))
        return new.access_token
```

Concurrency notes:

- File lock is `fcntl.flock` on a sibling `auth.json.lock` so we don't fight Codex CLI's own lock on `auth.json` itself.
- We do a *double-checked refresh* pattern (read-before-and-after lock acquire) so two callers can race in and only one performs the network call.
- Writes are atomic (`tempfile + os.replace`) so a crash mid-write can't leave Codex CLI with a corrupted file.

## Configuration

Per-instance, `ops/gateway.yaml`:

```yaml
default_brain: claude              # unchanged
triage: codex_api                  # NEW backend; "openrouter" / "claude-channel" still valid
openrouter_model: gpt-5.4-mini     # repurposed: triage model when triage=codex_api

channels:
  telegram:
    brain: codex_api               # route inbound DMs through Codex Responses
    model: gpt-5.4-mini            # default catalog: gpt-5.4-mini, gpt-5.4, gpt-5.5, gpt-5.3-codex

codex_auth:
  auth_file: ~/.codex/auth.json    # explicit, overridable for testing
  client_id_override: null         # null = read from JWT, else env CODEX_CLIENT_ID
  refresh_skew_seconds: 300        # refresh this far ahead of exp

# Spawn workers (`jc workers spawn --brain codex`) still use the existing
# `codex` CLI adapter — that path is untouched.
```

Defaults preserve current behavior — nothing changes unless an operator opts in.

## CLI surface

```
jc-codex-auth status
  Auth mode:       chatgpt (Plus)
  Account:         713d27cc-... (BNESIM, owner)
  Access token:    valid, expires in 6d 4h 12m
  Refresh token:   present (last rotated 2026-04-24T12:15:25Z)
  Auth file:       /home/lucamattei/.codex/auth.json (mode 600 ✓)

jc-codex-auth refresh
  Refreshed. New access token expires in 9d 23h 59m.

jc-codex-auth token
  eyJ...
```

## Failure modes

| Symptom | Action |
|---|---|
| `auth.json` missing | Print `codex login` instructions, exit 2 |
| `auth_mode != chatgpt` | Reject — API-key mode not supported, exit 2 |
| Network failure during refresh | Retry with exponential backoff (3x), then surface |
| `invalid_grant` / refresh expired/revoked | Log, fall back to legacy adapter (claude/codex CLI), surface a Telegram-actionable nudge to re-login |
| 401 on Responses API call | Force-refresh once and retry; on second 401, fall back |
| `auth.json` corrupted (e.g., concurrent write) | Reject with clear error; require manual `codex login` |

When a fallback fires, the gateway logs the brain switch so we can audit later. Triage and main-chat adapters share the fallback path — a code-API outage shouldn't take Rachel down.

## Security

- `auth.json` is mode 600. We preserve that on writes.
- Bearer tokens never log to stdout or cron mail. Status command shows time-to-expiry only, not the token itself.
- The token grant covers the entire ChatGPT account — treat it like a master credential.
- We do not transmit the refresh token anywhere except the OpenAI auth endpoint.

## Migration / rollout

1. Ship the module + CLI behind `enabled: false`.
2. Operators opt in by editing `ops/gateway.yaml`.
3. First production use: `triage.brain: codex_api` only — observe latency + cost for a week.
4. After stable: add `telegram_dm: codex_api` for chat. Group chats last.
5. If `gpt-4o-mini` triage proves accurate, document the switch and make it the default in a later release.

## Test plan

- **Unit:** JWT decode + expiry math, refresh request body shape, atomic write, file-lock contention (mock).
- **Integration with the live auth.json:**
  - `jc-codex-auth status` matches `cat ~/.codex/auth.json` + `last_refresh` field.
  - `jc-codex-auth refresh --force` produces a new `last_refresh` and a longer-lived token.
  - `jc-codex-auth token | jq -R 'split(".") | .[1] | @base64d | fromjson | .exp'` returns a future timestamp.
- **Wire-up smoke:**
  - One Responses API call against `gpt-4o-mini` from `lib/codex_auth/responses.py:complete()` returns text without 4xx/5xx.
  - With `triage.brain: codex_api`, an inbound Telegram message gets correctly routed.
- **Failure modes:** simulate 401 by tampering token, confirm refresh-then-retry path, confirm fallback to claude on second 401.

## Open questions for Luca

1. **Default model for triage.** The OpenAI catalog rotates fast — pick `gpt-4o-mini`, or wait for `gpt-5-nano`-class? Cheapest-with-reasoning is the right axis; I'd start with `gpt-4o-mini` and revisit at v2026.05.
2. **Group-chat opt-in.** Per-group config is more granular but harder to document. Start with chat-type level (`telegram_group_*`) and add per-group overrides in a later release?
3. **Cost monitor.** Add a simple monthly token-counter in `jc-codex-auth status`? Helpful for catching runaway loops; OpenAI's dashboard lags by hours.
4. **What happens when Codex CLI rotates the client ID?** We should snapshot the binary string at install time and store it in `state/codex_client_id` rather than living off a constant — agreed?
