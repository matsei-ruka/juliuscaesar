# `lib/company/` — the-company integration

Two integrations live here:

1. **Fleet client (`reporter.py`, `client.py`, `conf.py`, `alerts.py`, `approvals.py`, `cli.py`)**
   — long-running background reporter that POSTs gateway snapshots, worker
   DB deltas, conversation messages, alerts, and approvals. Buffered outbox,
   replay-on-reconnect. Owned by `GatewayRuntime`. Uses `requests`.

2. **Supervisor-driven worker reporter (`supervisor_reporter.py`,
   `supervisor_conf.py`)** — the subject of this README.

## Supervisor-driven worker reporter

### Purpose

Deterministic `worker.started` / `worker.finished` posts to the-company,
keyed off the supervisor's already-existing event lifecycle (snapshot →
finalize). Replaces the brain-must-remember-to-call-the-CLI pattern, which
drifted whenever the brain forgot.

The supervisor sees every running event already (it builds per-event
snapshots every tick and finalizes them on completion). The reporter just
hooks the same lifecycle so the dashboard mirrors the gateway's reality.

### Wiring

`lib/supervisor/runner.py:run_tick` calls:

- `report_started(...)` once per event on first sight (cached on
  `EventState.company_reported_started`).
- `report_finished(...)` once per event from `_finalize_completed` when the
  event leaves the active set in a terminal status (`done` / `failed` /
  `escalated`).

The reporter is constructed once per gateway process. Its `instance_boot_id`
(a UUIDv4) is reused for every call within that process — the-company
backend upserts on `(agent_id, instance_boot_id, remote_id)`, so a later
`finished` correctly closes a previously-opened `running` row.

### Enrollment

1. From the-company operator, obtain `agent_id` and an `api_key`.
2. On the instance host:
   ```bash
   mkdir -p state/company && chmod 700 state/company
   printf '%s' "<api_key>" > state/company/api-key
   chmod 600 state/company/api-key
   cp templates/init-instance/ops/the_company.yaml.example ops/the_company.yaml
   ```
3. Edit `ops/the_company.yaml`:
   ```yaml
   the_company:
     enabled: true
     api_url: http://192.168.14.112:8080
     agent_id: 4651933b-8ecd-4e23-992a-5e7cf56aafac
     api_key_file: state/company/api-key
   ```
4. Restart the gateway at the next natural opportunity. No mid-task
   restart is required by this PR — activation happens whenever the
   operator next bounces the process.

### Failure modes

- **the-company unreachable / 5xx / timeout** → reporter logs and returns
  False. Gateway tick continues. `company_reported_started` stays False on
  the failed event, so the next tick retries. If the backend is down for an
  extended period, events accumulate as unreported and the dashboard goes
  dark for this agent. **This is acceptable** — the gateway must not block
  on a downstream dashboard.

- **Config missing / `enabled: false`** → reporter is skipped entirely.
  Zero overhead per tick.

- **Bad api_key** → backend returns 401. Reporter logs and returns False,
  same retry behaviour as transient errors. Fix by replacing
  `state/company/api-key` and restarting.

- **API key file missing or empty after strip** → loader returns a disabled
  config. Reporter is silently skipped. Check supervisor logs for context.

### Hard constraints

- No new pip dependencies. urllib + json + stdlib only.
- 5-second socket timeout per call (`HTTP_TIMEOUT_SECONDS`). A wedged
  backend cannot stall the supervisor.
- The reporter never raises. Every error path is caught and logged.
- Channel delivery (cards in chat) is unaffected — same code path as today.
