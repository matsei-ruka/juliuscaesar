# Agent Self-Discovery — Operating Spec

Status: Draft — spec-only PR, no code in this branch
Date: 2026-05-27
Author: Noah
Repo: `juliuscaesar` (this PR) + `the-company` (companion PR for the backend route)
Related: `docs/specs/company-inbox-channel.md` (the channel that motivates this)

---

## 0. What this spec is, and is not

This spec defines how an agent's gateway figures out *its own UUID inside
the-company catalog*, in the situation where that UUID wasn't persisted at
registration time. Today the `company-inbox` channel falls back to
`instance_id` (a SHA256 of the instance directory path) as the agent
identifier, which is a string the-company has never seen and never will —
so every request hits `400 Bad Request` and the channel quietly does
nothing forever.

It is **not** a new authentication scheme. The bearer token (`COMPANY_API_KEY`)
already identifies the agent on the wire. This spec just exposes a way for
the agent to ask the-company "who am I, by UUID?" — and to persist the
answer locally so the next boot doesn't have to ask again.

## 1. Motivation — the incident on 2026-05-27

We tried to deliver a task to Ethan via the `company-inbox` channel. The
channel was registered as enabled, the agent was online, the backend
correctly listed one pending task in `/api/agents/{ethan_uuid}/inbox`.

Nothing happened.

Reading the backend logs revealed the actual request the channel was making:

    GET /api/agents/40414770…cc708c15/inbox  →  400 Bad Request

That long hex string is the SHA256 of `/home/jc/ethan_zhang` — Ethan's
instance directory. The channel's `_agent_id()` helper had fallen back to
`lib/company/conf.py:instance_id()` because Ethan's `.env` didn't contain
a `COMPANY_AGENT_ID` line.

Why was `COMPANY_AGENT_ID` missing? Because Ethan was registered before
the `company-inbox` channel shipped. The original `jc-company register`
flow (which still runs for re-registrations) never wrote that key. PR #64
added the write at register time, but only for *fresh* enrollments — there
is no path that gives existing agents the value.

Result: every agent that registered before 2026-05-26 is in this state.
That's all 20 of them. When `company-inbox` gets enabled on any of them,
the same 400 loop starts and the channel never injects a single task.

The fix has to come from the agent side (the channel must discover its
own UUID at boot) *and* from the backend (the agent has to be able to
ask without already knowing the answer).

## 2. Scope

### 2.1 In scope

- A new backend endpoint `GET /api/agents/me` that resolves the caller's
  bearer token to its agent record and returns the canonical fields
  (`id`, `slug`, `company.id`, `company.slug`, `display_name`).
- A boot-time discovery step in the `company-inbox` channel: read
  `COMPANY_AGENT_ID` from `.env`; if missing, call `/api/agents/me`,
  write the result to `.env`, then start polling.
- A clear failure mode when discovery fails: hard WARN to the gateway log
  and the supervisor health surface, and **do not poll**. No more silent
  400s.

### 2.2 Out of scope (deferred)

- Changes to the existing registration flow. `jc-company register` keeps
  writing `COMPANY_AGENT_ID` on first enrollment exactly as PR #64 did.
- Auto-rotation of the API key. The spec assumes the bearer is still valid;
  if it isn't, the existing 401 → reload-from-.env path stays as is.
- Re-discovery on every boot. Once `COMPANY_AGENT_ID` is in `.env`, the
  channel trusts it and skips the `/me` call until the next 401-reload.

## 3. Backend — `GET /api/agents/me`

### 3.1 Route shape

    GET /api/agents/me
    Authorization: Bearer <api_key>

    200 OK
    {
      "id":         "6483bd0a-f750-4388-b9f5-52b02d0491ad",
      "slug":       "ethan_zhang",
      "display_name": "Ethan Zhang",
      "company": {
        "id":   "6a277db2-593c-4695-b64a-c94423cdf92a",
        "slug": "omnisage"
      }
    }

    401 Unauthorized — bearer missing, malformed, or revoked.
    403 Forbidden    — bearer resolved to a user, not an agent. /me is
                       for agent callers only; session users go through
                       /api/agents/{id} where {id} is the URL fragment.

### 3.2 Implementation note (informational)

`current_agent` already exists in `company.deps`. It resolves a bearer
token to an `Agent` ORM row and raises 401 on failure. The route is one
dependency line + a serializer to `AgentOut`. Lives at
`backend/company/routes/agents.py`.

### 3.3 Ordering

Must register **before** the `/{agent_id}` parameterised route in the
same router. Otherwise `me` is parsed as a literal UUID and 400s. Mirrors
the `/api/tasks/counts` ordering already in main.

### 3.4 Schema

No new schema — reuses `AgentOut` (already in `company/schemas.py`).
This keeps the catalog one consistent shape and lets the channel reuse
the existing `AgentWire` typing on the agent-side `CompanyClient`.

## 4. Agent — channel boot

### 4.1 New flow at `company-inbox` start

    1. Load `CompanyConfig` from .env via `lib/company/conf.py`.
    2. If `cfg.agent_id` is set and non-empty → use it. Start polling.
    3. Else → call `GET /api/agents/me` with the bearer.
       a. 200 → write `COMPANY_AGENT_ID=<id>` to .env atomically
                (temp file + rename, preserve mode 0600).
                Reload `CompanyConfig`. Start polling.
       b. 401 → existing auth-failure path: try to reload .env once,
                if unchanged enter the degraded loud-WARN state. Do not poll.
       c. 403 → impossible by construction (agent caller, valid bearer)
                but if it happens: log + degraded + do not poll.
       d. Network error → degraded poll interval (cfg.poll_interval × 4),
                retry the discovery call, do not poll inbox until it
                succeeds.

### 4.2 What "do not poll" means

When discovery fails persistently, the channel must **not** call
`/api/agents/{instance_id}/inbox` with the fallback SHA256 — that's the
exact silent-400 bug this spec exists to kill. Instead:

- Increment `consecutive_failures`.
- After 3 consecutive failures, surface a single WARN on the gateway log
  and the supervisor health field (`auth_valid: false, last_error: 'agent_id discovery failed'`).
- Keep the discovery retry on the degraded cadence. Once it succeeds,
  resume normal polling. Once-loud-then-silent, per spec §7 of
  `company-inbox-channel.md`.

### 4.3 Removing the fallback

`lib/gateway/channels/company_inbox.py:_agent_id()` currently returns
`instance_id` if `cfg.agent_id` is missing. **This fallback goes away.**
The function becomes a simple accessor; the discovery step at boot is
the only path that populates the value.

The `instance_id` function in `lib/company/conf.py` stays — it's still
used by the supervisor reporter as an idempotency key for snapshot
publishes, where the SHA256 shape is intentional.

## 5. Idempotency & re-runs

- The `/me` response is the same for every call as long as the bearer
  is valid. Persisting it once and reusing it costs nothing.
- If the operator later rotates the API key, the existing 401 reload
  path kicks in. The new key still belongs to the same agent (rotation
  doesn't change `agent.id`), so the persisted `COMPANY_AGENT_ID` stays
  correct.
- If the operator re-enrolls the agent under a different slug (rare,
  but possible after a name change), `register` rewrites both
  `COMPANY_API_KEY` and `COMPANY_AGENT_ID` in .env — no `/me` call
  needed in that case.

## 6. Failure modes named

1. **Network unreachable at boot.** Discovery enters retry-with-backoff.
   The channel does not poll the inbox until discovery succeeds.
2. **401 from `/me`.** Bearer is revoked. Same handling as 401 from
   `/inbox` today — reload .env once; if unchanged, degraded loud.
3. **`.env` write fails.** Disk full, permission denied, etc. The channel
   logs the error, keeps the discovered UUID in memory for this run,
   continues polling. Next boot will rediscover and re-attempt the write.
   No silent corruption — the in-memory value is correct.
4. **Backend serves a wrong UUID.** Impossible by construction
   (bearer-to-agent is a single-row lookup in postgres) but if it ever
   happens, the next poll returns `Forbidden` or `Not Found` and the
   channel surfaces the mismatch loudly.
5. **Two gateways sharing a key.** They both discover the same agent_id;
   the queue dedup index (`idx_events_dedup`) already absorbs duplicate
   injects. The catalog still sees a single agent (correct, single bearer).

## 7. Test plan

Backend (the-company):

1. `GET /api/agents/me` with a valid agent bearer → 200 + correct fields.
2. Same with no bearer → 401.
3. Same with a session cookie (user, not agent) → 403.
4. Same with a revoked bearer → 401.
5. Regression: `/me` registered before `/{agent_id}` — assert via
   route-table inspection that the literal `me` cannot be parsed as a
   UUID under any path resolution.

Agent (juliuscaesar):

1. Fresh `.env` with `COMPANY_API_KEY` but no `COMPANY_AGENT_ID`. Start
   the channel. Assert: one `/me` call, `.env` rewritten, mode 0600
   preserved, polling begins.
2. `.env` already has `COMPANY_AGENT_ID`. Start the channel. Assert:
   no `/me` call, polling begins immediately.
3. Backend down at boot. Assert: discovery retries on the degraded
   cadence, channel does NOT call `/inbox`, no 400 line in the log.
4. Backend returns 401 to `/me`. Assert: reload-from-.env path triggers,
   if unchanged, degraded loud, no inbox call.

## 8. Sequence

This PR is **specs only.** No code change in this branch. Implementation
follows in two parts:

1. Backend route (`the-company` PR) — `routes/agents.py` adds the `/me`
   handler. Schema unchanged. One test added.
2. Agent boot path (`juliuscaesar` follow-up PR) — `company_inbox.py`
   loses the fallback, gains the discovery step. `register.py` is
   untouched.

Each part is independently reviewable. The agent change is no-op until
the backend route is deployed — graceful: if `/me` returns 404, the
channel goes degraded loud (same as discovery failure), and we get a
clear signal that the deploy order was wrong.

## 9. Open questions

- **Q1 — What if the agent has no row?** Possible if the bearer is valid
  but its `agent_id` foreign key points to a deleted row. The proposal:
  treat as 401. Same observable behaviour for the operator
  (`api_key_invalid`), and the only fix is re-register either way.
- **Q2 — Slug uniqueness.** `/me` returns `slug` for convenience but
  slugs are unique per-company, not globally. If a downstream tool keys
  off slug instead of id it'll break for multi-company fleets. Mitigated
  by the company envelope in the response — callers that key off the
  pair `(company.slug, agent.slug)` are safe.
- **Q3 — Discovery on every restart vs. cache forever.** Currently the
  spec says "cache in .env once, trust forever." Alternative: rediscover
  on every boot to catch a stale `.env` after an agent move. Tradeoff:
  one extra HTTP call per gateway start vs. defending against operator
  error. Default in this spec: cache forever; operator-driven re-discovery
  (delete the line + restart) is fine for now.
