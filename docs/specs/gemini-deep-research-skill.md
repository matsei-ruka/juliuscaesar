# Gemini Deep Research skill

Status: **draft, awaiting operator review** — do not implement before approval.

## Goal

Add a pre-shipped framework skill that lets any JC instance (Rachel, Marco,
Elliot, future personas) run a Gemini Deep Research query through Luca's
Gemini Advanced subscription via browser automation — zero API cost, full
consumer-app quality.

Trigger phrase from a persona: "do a deep research on X". Today personas have
to fall back to Tavily/Firecrawl/Brave (good but not Deep-Research grade) or
hit the Gemini API ($5–25 per query). After this skill, they spawn a job, get
a 5–10 minute report back through `jc-events`, summarize in Rachel's voice.

## Scope

New code:

- `templates/init-instance/skills/gemini-deep-research/SKILL.md` — skill
  playbook (the prompt the agent reads).
- `bin/jc-research` — CLI binary (Python). Subcommands: `login`, `run`,
  `start`, `status`, `result`, `cancel`, `list`.
- `lib/skills/gemini_deep_research/` — Python module. Submodules:
  `__init__.py`, `runner.py` (browser-use orchestration), `auth.py`
  (profile management), `jobs.py` (async state), `selectors.py`
  (DOM hints + UI version pin), `errors.py` (typed exit codes).

Edits:

- `bin/jc-skills` — add `SkillSpec("gemini-deep-research", …)` to
  `PRE_SHIPPED`; add `test_gemini_deep_research` tester to `TESTERS`.
- `bin/jc` — register `research` subcommand in the dispatcher and help text.
- `install.sh` — append `jc-research` to `BINARIES`; add `browser-use` and
  `playwright` to `DEPS`; run `playwright install --with-deps chromium`
  post-install (idempotent).
- `templates/init-instance/skills/Index.md` — add row.
- `CHANGELOG.md` — entry.

Not in scope:

- Bypassing Gemini's TOS, scraping anonymous results, multi-account
  rotation, or anything that hits the Gemini API. This skill consumes the
  existing logged-in subscription only.
- Voice/tone for user-facing strings — the skill emits CLI logs (operator)
  and `jc-events` payloads (consumed by the gateway, which routes through
  the normal persona pipeline). No persona-voice copy lives in skill code.

## Design

### Why browser-use over Playwright-only

Pure Playwright with hardcoded selectors breaks every time Google touches
the Gemini DOM. `browser-use` (LLM-driven agent on top of Playwright) reads
the page and decides clicks, surviving most UI churn. Trade-off: it spends
~$0.05–0.20 in cheap-model tokens per run (e.g. `openai/gpt-4o-mini` or
`anthropic/claude-haiku-4-5` via OpenRouter). Net win: $0.20 vs. $5–25 of
direct Gemini API. We pin a model env (`JC_RESEARCH_NAV_MODEL`) so cost is
predictable and operator can swap.

`selectors.py` ships best-effort Playwright fast-path selectors. If they
match, we skip the LLM agent for the deterministic clicks (open Gemini →
switch to Deep Research mode → submit prompt). If they fail, fall back to
the browser-use agent for the failing step. Adaptive, cheap when the UI is
stable.

### Auth: persistent Chrome profile

Profile dir: `${XDG_CONFIG_HOME:-$HOME/.config}/jc-skills/gemini-profile/`.
One profile per host, shared by all instances on that host (Rachel +
Marco on the same VM both use the same Google session — that's correct,
they're driving the *same* Luca subscription).

Login flow (one-shot per host):

```
$ jc research login
[opens headed Chromium → accounts.google.com]
[operator signs in, accepts the standard "stay signed in" cookie]
[operator closes the window or presses Enter in terminal]
✓ profile saved. Test:  jc research run "ping" --dry-run
```

Run flow uses the same profile dir with `--user-data-dir=...`. Cookies
persist across reboots. When Google forces re-auth (typically every
~14 days or on suspicious-IP heuristics), the next run errors with exit
code 10 → operator re-runs `jc research login`.

**Rejected: cookie injection.** `__Secure-1PSIDTS` rotates aggressively,
extraction needs CDP access on a separate browser, and consent flags
(`SOCS`, `NID`) are easy to miss. Persistent profile is one Chromium-shaped
blob that Just Works.

### Concurrency

Chromium locks `user-data-dir` — only one process per profile at a time.
We serialize with `fcntl.flock` on
`<profile-dir>/.jc-research.lock`. A second concurrent run waits up to
60 s on the lock then fails with exit code 16 (busy). Async jobs respect
the lock too, so the same lock prevents two parallel deep-research runs
for the same Google account on the same host.

### Async + gateway-friendly

Deep Research takes 3–10 min. Gateway must not block.

- `jc research run "<q>"` — synchronous; for operator/CLI use only.
- `jc research start "<q>"` — detaches via `subprocess.Popen` with
  `start_new_session=True`, writes
  `state/research/jobs/<job_id>.json` (status=`running`, pid, started_at,
  query), prints `<job_id>` on stdout, returns immediately.
- Background process writes
  `state/research/<job_id>/report.md`, `meta.json`, `run.log`,
  `screenshot.png`. On completion drops
  `state/events/research-<job_id>.json` so the gateway's `jc-events`
  channel picks it up and synthesizes a Telegram message via the persona.
- `jc research status <id>` / `jc research result <id>` /
  `jc research cancel <id>` for inspection.

`jc-events` event payload:

```json
{
  "event_id": "research-<job_id>",
  "event_type": "research.completed",
  "notify_chat_id": "<luca_chat_id from CHATS.md>",
  "notify_channel": "telegram",
  "job_id": "<job_id>",
  "query": "<original query>",
  "report_path": "<absolute path>",
  "status": "ok|failed",
  "duration_seconds": 312,
  "sources_count": 24
}
```

The existing `JcEventsChannel._render_content` handles
`worker.completed`; we extend it (small addition, ~20 LoC) to also render
`research.completed` into a synthesis prompt — the persona reads the
report and produces a Rachel-voiced summary in chat.

### Browser-use flow

Pseudo-code, deterministic-first:

```
1. launch Chromium with user-data-dir = profile, headless if !--debug
2. goto https://gemini.google.com/app
3. wait for [data-test-id="chat-input"] OR sign-in URL
   - if sign-in URL → exit 10 (auth_required)
4. open model selector (selectors.MODEL_SWITCH); pick "Deep Research"
   - if not present in menu → exit 12 (deep_research_unavailable)
5. type query into chat input; submit
6. wait for plan card (selectors.PLAN_CARD); click "Start research"
7. poll for completion sentinel — research panel switches from
   "Researching…" to "Research complete" or the export button appears.
   timeout: --max-wait (default 900s)
8. click "Export → Markdown" (selectors.EXPORT_MD); capture clipboard or
   download
9. parse sources from sidebar (selectors.SOURCES_LIST)
10. write report.md + meta.json + screenshot.png
11. exit 0
```

If any step's deterministic selector misses, we hand the same step to a
browser-use Agent with a tight goal ("on this page, switch the model to
Deep Research") and a 30 s budget. Failure of the fallback → log the page
DOM hash + screenshot, exit with the closest-fit code.

### Failure modes and exit codes

| Code | Reason | Recovery |
|------|--------|----------|
| 0 | OK | — |
| 10 | auth required (sign-in redirect) | `jc research login` |
| 11 | captcha / unusual-traffic challenge | manual: open profile in headed mode, solve, retry |
| 12 | Deep Research unavailable (region / no-sub) | check Gemini Advanced subscription |
| 13 | quota / rate limit hit | wait + retry; surface message to operator |
| 14 | UI selector + browser-use fallback both failed | bump `selectors.py`; attach screenshot to issue |
| 15 | browser crash / unrecoverable Playwright error | retry; if persistent, reset profile |
| 16 | concurrency lock timeout | wait for in-flight job |
| 17 | invalid input / arg parse | fix command |

Every non-zero exit writes a structured `meta.json` with code, message,
last URL, and a screenshot.

### Output format

`state/research/<job_id>/report.md`:

```markdown
---
query: "Compare eSIM market share UAE vs KSA 2025"
job_id: 01HXYZ...
started: 2026-05-10T14:32:00Z
finished: 2026-05-10T14:39:14Z
duration_seconds: 434
model: gemini-2.5-deep-research
sources_count: 27
exit_code: 0
---

# <Gemini-generated title>

<full markdown report exactly as Gemini exported it, sources kept inline>

## Sources

1. [Title](url) — domain.com — accessed 2026-05-10
2. ...
```

Sidecars: `meta.json` (machine-readable), `screenshot.png` (final page),
`run.log` (browser-use trace, redacted of cookies).

### Invocation API

CLI (operator + agent):

```
jc research login
jc research run "<query>" [--out PATH] [--max-wait 900] [--debug]
jc research start "<query>"           # → prints job_id
jc research status <job_id>           # → JSON: {status, pid, ...}
jc research result <job_id>           # → cats report.md
jc research cancel <job_id>
jc research list [--limit 20]
```

Python (workers, heartbeat, in-process callers):

```python
from jc.skills.gemini_deep_research import run, start, status, result

job = start("Compare X vs Y", instance_dir=Path.cwd())
# ...
state = status(job.job_id)
if state.status == "ok":
    text = result(job.job_id)
```

### Persona-side trigger (SKILL.md)

The skill playbook tells the agent: when the user asks for a deep dive /
multi-source / "deep research" task that exceeds Tavily/Firecrawl scope,
call `jc research start "<query>"` (NOT `run` — non-blocking) and
acknowledge in chat with "kicked off, ~5 min, will ping when ready". The
gateway's `jc-events` synthesis on `research.completed` handles the
follow-up reply, so the agent does not need to poll.

Skill description (frontmatter) makes this trigger explicit so the brain
picks it over Tavily for the right query shape.

### Distribution

- New host or new instance: `./install.sh` from a fresh clone or
  `git pull && ./install.sh` on existing → installs `jc-research` shim
  + Python deps + Chromium.
- Existing instance dirs: `jc skills sync` copies the new
  `gemini-deep-research/` skill folder + Index row.
- `jc-init` already calls `jc skills sync`-equivalent on scaffolding;
  verify and add if missing.
- Per-host login: `jc research login` once. No per-instance login —
  same Google account, same profile.

### Security and privacy

- Cookie blob in profile dir is `chmod 700` enforced on every run
  (`auth.py` checks).
- `run.log` redacts `Cookie:` and `Authorization:` headers.
- Report contents are not auto-shared; only the agent's synthesized
  reply goes out, and that goes through the standard persona/external
  filter.
- `JC_RESEARCH_NAV_MODEL` requires `OPENROUTER_API_KEY` in instance
  `.env` if the browser-use fallback path triggers — credentials never
  written to skill files.

### Operational notes — fleet

Multi-host fleet implication: Rachel + Marco on `192.168.3.246` share
one profile; Elliot on `.241` needs its own login. Documented in
SKILL.md and the host-onboarding runbook.

If the operator wants to forbid a given instance from spending the
subscription (e.g. a low-trust persona), set
`JC_RESEARCH_DISABLED=1` in that instance's `.env` — `jc-research`
exits 17 if set.

## Test plan

### Unit

- `tests/skills/test_gemini_runner.py`:
  - prompt builder produces the expected text given query
  - output parser strips Gemini sidebar HTML, extracts sources
  - exit-code mapper covers every failure path
  - browser-use agent invocation is mocked (we don't drive Chromium in CI)
- `tests/skills/test_gemini_jobs.py`:
  - `start` writes job file, returns id
  - `status` reads back state, handles missing files
  - lock acquisition + timeout

### Integration (manual, gated by env)

`tests/integration/test_gemini_deep_research_live.py` — opt-in via
`JC_RESEARCH_LIVE=1`:

1. Asserts `jc research login` profile exists and is fresh (<14d).
2. Runs `jc research run "What is 2+2 in arithmetic?"` with a 120 s
   `--max-wait` (Deep Research mode handles trivial prompts in ~30 s).
3. Asserts: exit 0, `report.md` exists, contains "4", at least one
   source URL, `meta.json.duration_seconds < 120`.

### Fleet acceptance

1. Rachel: `./install.sh && jc skills sync && jc research login &&
   jc research run "test"` → green.
2. Marco: `jc skills status` shows `gemini-deep-research` present;
   `jc research run "test"` works (same profile, no separate login).
3. Drop a `state/events/research-<id>.json` payload manually → confirm
   gateway picks it up and Rachel replies in TG with a synthesized
   summary.

## Open questions for operator

1. **Profile sharing across hosts** — out of scope for v1 (each host
   logs in once). Worth a follow-up to sync cookies via vault if more
   hosts come online?
2. **Research budget** — Gemini Advanced has a daily Deep Research
   quota. Should we add a soft cap (e.g. 10 runs/day) and warn the
   persona? v1 ships uncapped; we discover the real limit and add it
   in v1.1.
3. **Headless vs. headed** — default headless. `--debug` flips to
   headed. OK?
4. **Skill name** — `gemini-deep-research` vs. just `deep-research`
   (so future Claude/Perplexity backends can plug in)? I lean toward
   the latter, with `--backend gemini` as the default.

Operator: please confirm or correct the four points above before I
implement. Everything else implements as specified.
