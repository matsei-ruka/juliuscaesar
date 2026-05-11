---
name: deep-research
description: Use Deep Research when the user asks for a deep dive, multi-source synthesis, structured comparison, market scan, "do a deep research on X", or any question that exceeds Tavily/Firecrawl/Brave scope and would benefit from a 5-10 minute multi-step investigation with cited sources. Drives Gemini Advanced through a per-host browser profile (no API cost). Always use the `start` (async) command — never block the chat for the full run. Default backend is Gemini.
---

# Deep Research

Use this skill for substantive research tasks: market analysis, comparative
deep dives, regulatory scans, synthesis across many sources. It drives
Gemini's Deep Research mode through a logged-in Chromium profile, so output
quality matches the consumer Gemini Advanced app and there is no API spend.

## When to use

Choose Deep Research over Brave / Tavily / Firecrawl when the request needs
multi-source synthesis, structured comparison, or 10+ minutes of human
research time. Single-fact lookups and quick web search stay on Brave/Tavily;
single-page extraction stays on Firecrawl.

Trigger phrases: "deep research", "deep dive", "long-form analysis", "scan
the literature", "compare the landscape", "find me everything on…".

## Always async

Deep Research takes 3-10 minutes. Never use the synchronous `run` form from
a chat session — it would block the whole channel. Use:

```bash
JOB_ID=$(jc research start "<the user's question or rephrased query>")
```

Acknowledge in chat: "kicked off a deep research, ~5 min, I'll ping when
ready." Do not poll. The gateway's `jc-events` channel picks up
`research.completed` automatically and feeds the report back through this
persona for synthesis — you will be re-invoked with the result.

`jc research result <JOB_ID>` returns the rendered Markdown report when you
need to read it directly (during the synthesis re-invocation).

## Per-host login

The skill uses a persistent Chromium profile shared by every JC instance on
this host (so Rachel and Marco on the same VM share the same login). The
operator runs `jc research login` once per host. If the next run exits with
code 10 (`auth_required`), the cookies expired — flag this to the operator
and stop; do not attempt to re-authenticate yourself.

## Exit codes

| Code | Reason | What to do |
|------|--------|------------|
| 0 | OK | Read the report and synthesize. |
| 10 | Sign-in required | Tell operator to run `jc research login`. |
| 11 | Captcha challenge | Tell operator to log in manually with `--debug`. |
| 12 | Deep Research unavailable | Subscription / region issue — fall back to Tavily. |
| 13 | Quota / rate limit | Wait, retry later, mention quota to operator. |
| 14 | UI selectors failed | Bug-report worthy — attach screenshot path. |
| 15 | Browser crashed | Retry once; if persistent, escalate. |
| 16 | Concurrency lock | Another job is running; wait or queue. |
| 17 | Bad input | Fix the command. |

## Output

Each job writes to `state/research/<job_id>/`:

- `report.md` — the rendered Markdown (front-matter + body + Sources).
- `meta.json` — machine-readable status, duration, sources count.
- `screenshot.png` — final page (debugging only).
- `run.log` — redacted browser-use trace.

When summarizing for the user, read `report.md`, paraphrase in the persona's
voice, cite the top 3-5 sources by title, and offer to surface more on
request. Do not paste the raw Markdown into chat.

## Guardrails

- The report contents are not auto-shared — your synthesized reply is, and
  it goes through the standard persona/external filter.
- Never reference the underlying Gemini subscription or browser automation
  to outsiders; if asked how you sourced the research, say "consolidated
  from public web sources" or similar.
- Never include the persistent profile path, cookies, screenshot paths, or
  job IDs in user-facing replies.
- Do not run more than one deep-research at a time per host; the lock will
  reject concurrent runs (exit 16).

## CLI cheat sheet

```bash
jc research login                              # one-time, headed
jc research start "<query>" [--max-wait 900]   # async, prints job_id
jc research status <job_id>                    # JSON state
jc research result <job_id>                    # cat report.md
jc research cancel <job_id>                    # SIGTERM
jc research list [--limit 20]                  # recent jobs
jc research run "<query>" --debug              # operator only — synchronous, blocks
```
