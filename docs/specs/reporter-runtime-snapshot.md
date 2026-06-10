# Reporter Runtime Snapshot — Operating Spec

Status: Draft — spec-only PR, no code in this branch
Date: 2026-05-27
Author: Noah
Repo: `juliuscaesar` (this PR) + `the-company` (companion PR for the merge logic)
Related: `docs/specs/company-inbox-channel.md`, the-company `docs/specs/the-company-v2.2-task-graph.md`

---

## 0. What this spec is, and is not

This spec defines a small extension to the supervisor reporter: each
snapshot it publishes now carries a `runtime` block with the current
hostname, primary network IP, framework version (with commit SHA),
supervisor PID, and uptime. The backend merges that block into the
agent's `deployment.runtime` field so the dashboard reflects reality
instead of the value an operator typed in at enrollment.

It is **not** a new transport. The snapshot publish path is the same.
It is **not** a discovery mechanism: an agent's identity is still its
UUID, set at register time. Runtime data is observational, not
authoritative.

It does **not** replace the `deployment` field as it exists today —
that field stays for operator-curated facts (kind, pve_node, ssh_port_hint,
etc.). Runtime data lands in a nested sibling key so the operator's
intent is preserved.

## 1. Motivation — the dogfood found two stale facts

Two days ago we moved Ethan from `192.168.14.240` to `192.168.14.115`.
This morning the catalog still showed the old host. The framework version
showed `2026.05.17.09` while the binary on disk reported
`2026.05.27.01+73bf58d`. Both were wrong, and both became wrong in the
same way: they're written once (at register) and never updated.

We discovered the IP gap by accident — comparing the SHA256 of
candidate instance directories against a request the reporter was
making. Funny once. Not OK as the discovery mechanism for the fleet.

The dashboard is supposed to be the operator's source of truth for
"who lives where, running what." Today it lies. This spec closes the
gap.

## 2. Scope

### 2.1 In scope

- A new `runtime` block in the snapshot payload published by the
  supervisor reporter. Fields below in §3.
- A new merge rule on the backend snapshot ingest path:
  `agent.deployment.runtime ← snapshot.runtime`. Other keys in
  `deployment` are untouched (deep-merge at the top level, *not* a
  full replace).
- A small UI tweak in the dashboard `AgentDetail` page so the runtime
  values render as a distinct block from operator-set deployment data.
  (Optional in v1; the data is on the JSON either way.)

### 2.2 Out of scope (deferred)

- Pushing runtime data more frequently than the existing snapshot
  cadence. Snapshots already publish every supervisor tick (~30s).
  Adding a higher-frequency channel for the same data is premature.
- Renaming the existing `deployment` field. The shape stays
  backward-compatible — old operator-set keys keep their meaning.
- Validating the IP / hostname for tampering. The bearer authenticates
  the agent; what the agent reports about itself is what we trust.
  A compromised agent can already say anything; that's a higher-level
  problem than this spec.

## 3. The `runtime` block

### 3.1 Schema (added to existing `gateway_snapshot` payload)

    runtime:
      hostname: str               # socket.gethostname()
      primary_ip: str             # see §3.2 for selection rule
      framework_version: str      # what lib/company/version.py returns
      framework_commit: str | null  # the git SHA portion, parsed
      supervisor_pid: int | null  # from state/supervisor/jc-supervisor.pid
      uptime_seconds: int         # process uptime of the gateway
      reported_at: str (ISO 8601) # client-side timestamp at capture

### 3.2 `primary_ip` selection

A host can have many network interfaces. The reporter picks **the IP
of the interface used to reach `COMPANY_ENDPOINT`** — i.e. the source
IP that would carry the snapshot request itself. Mechanically this is
a UDP-socket-bind-to-the-endpoint-without-sending trick:

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((host_from_endpoint, 80))
    ip = s.getsockname()[0]
    s.close()

Failure mode: if the endpoint hostname doesn't resolve at startup
(unlikely; the reporter wouldn't be running at all if it didn't), the
selection falls back to `socket.gethostbyname(socket.gethostname())`.
If that also fails, `primary_ip` is set to `null` and the snapshot
proceeds without it.

### 3.3 `framework_version` and `framework_commit`

Today `lib/company/version.py:framework_version()` returns a string
like `2026.05.27.01+73bf58d` (date + tag + git short SHA). The reporter
already sends this as a top-level field. This spec splits it:

- `framework_version` stays as the full string (backward compatible).
- `framework_commit` is the SHA portion, extracted by parsing on the
  `+` separator. Convenient for dashboards that want to diff agents.

If the parse fails (no `+`), `framework_commit` is `null`.

### 3.4 `supervisor_pid`

Read from `state/supervisor/jc-supervisor.pid`. If the file is missing
(supervisor not running, somehow), the field is `null`. The reporter
does not try to discover the supervisor any other way — it's not its
job to be a process supervisor of the supervisor.

### 3.5 `uptime_seconds`

`time.time() - start_time` of the reporter's own process. Approximates
"how long has this gateway been up since the last restart." Operators
use this to spot agents that loop-crash without anyone noticing.

## 4. Backend — merge into `deployment.runtime`

### 4.1 The merge rule

Today the snapshot ingest endpoint (`POST /api/agents/{id}/snapshot`)
writes the snapshot to `gateway_snapshots` but does **not** touch
`agents.deployment`. This spec changes that:

    on snapshot received:
        snapshot_row = insert into gateway_snapshots(...)
        if 'runtime' in payload:
            agent.deployment = deep_merge_top_level(
                agent.deployment,
                {'runtime': payload['runtime']}
            )

Deep-merge at the top level means: any key in `agent.deployment` other
than `runtime` is preserved as-is. The `runtime` key is replaced
wholesale by each snapshot (no nested merge inside — the reporter is
authoritative on its own runtime values; if the reporter stops sending
`primary_ip`, the field disappears, which is the correct signal).

### 4.2 Why a separate key

The current `deployment` field mixes operator intent and observation.
After this spec, the convention is:

    deployment.kind:           operator-set (bare-metal, proxmox-vm, …)
    deployment.host:           operator-set (the canonical hostname)
    deployment.ssh_port_hint:  operator-set
    …                          (anything else the operator added)
    deployment.runtime.*:      reporter-set, last-write-wins

So an operator can keep declaring "Ethan lives on `192.168.14.115`"
(by intent) while the runtime block says "today he's actually running
from `192.168.14.115`" (by observation). When the two diverge, the
dashboard can highlight the drift.

In v1 we don't render the divergence specially — that's UI polish. But
the data shape makes it cheap to add later.

### 4.3 Authorization

No change. The snapshot endpoint already authenticates via bearer, and
the agent can only patch its own row. `deployment.runtime` is written
by the same call.

A `company_admin` editing `deployment` via `PATCH /api/agents/{id}`
must not stomp `runtime` — and conversely the snapshot ingest must not
stomp the rest of `deployment`. The deep-merge-at-top-level rule
guarantees both directions.

## 5. Frontend — minimal display

### 5.1 AgentDetail block

The `AgentDetail` page already renders a "Deployment" section. After
this spec it gets a sibling "Runtime" block sourced from
`deployment.runtime`:

    Runtime (auto)
      Host:       ethan-zhang  (192.168.14.115)
      Framework:  2026.05.27.01+73bf58d
      Uptime:     8h 14min
      Reporter:   30s ago

The "auto" label tells the operator this block is observation, not
intent. No edit affordance — it's not editable.

If `deployment.runtime` is missing (old agents pre-this-spec), the block
shows "Reporter hasn't sent runtime info yet" and a hint to restart the
gateway. Same shape as the existing "no snapshot yet" empty state.

### 5.2 Drift indicator (deferred)

Future polish: when `deployment.host != deployment.runtime.primary_ip`
(or hostname), badge the runtime block so the operator notices the
drift. Not in v1.

## 6. Failure modes named

1. **Hostname lookup fails.** `socket.gethostname()` raises. Reporter
   sets the field to `null` and continues. Snapshot still publishes.
2. **No network interface up.** `primary_ip` selection raises. Same
   handling: `null`, continue.
3. **Backend snapshot endpoint rejects the new `runtime` block as
   unknown.** Won't happen if the backend PR ships first; if the
   backend PR is delayed, the field is just ignored (existing schema
   tolerates unknown keys per pydantic config). The reporter doesn't
   need to gate on backend version.
4. **`agent.deployment` is `null`.** Fresh agent never had a `deployment`
   patch. Backend initialises to `{}` before the merge.
5. **Concurrent snapshot + admin PATCH on `deployment`.** Last-write-wins
   on the top-level key set. Admin's `deployment.kind` change can't be
   overwritten by a snapshot because the snapshot only writes
   `deployment.runtime`. Symmetric protection.

## 7. Test plan

Agent (juliuscaesar):

1. Reporter publishes a snapshot. Assert the payload includes a `runtime`
   block with `hostname` and `framework_version` set, `primary_ip` set
   when the network is up.
2. Force `socket.gethostname` to raise via monkeypatch. Assert the
   snapshot still publishes, with `hostname: null` and other fields intact.
3. Force `primary_ip` selection to raise. Assert `primary_ip: null`,
   no crash.
4. Reporter starts twice in 60s (cold start, restart). Assert
   `uptime_seconds` resets to the new boot's elapsed time.

Backend (the-company):

1. POST a snapshot with a `runtime` block. Assert `agent.deployment.runtime`
   is set to exactly that block.
2. POST a snapshot **without** a `runtime` block on an agent that already
   has one. Assert the existing `deployment.runtime` is left alone (snapshot
   without the key is a no-op for the merge).
3. Operator PATCHes `agent.deployment.kind = "proxmox-vm"`. Then a
   snapshot lands with `runtime`. Assert both keys coexist; neither
   stomps the other.
4. Operator PATCHes `agent.deployment = {}` (explicit reset). Assert the
   next snapshot re-populates `deployment.runtime` cleanly.

UI (frontend):

1. Agent with `deployment.runtime` populated. Assert the Runtime block
   renders with all fields.
2. Agent with no `deployment.runtime`. Assert the empty-state hint
   renders, no JS errors.

## 8. Sequence

Spec-only PR in `juliuscaesar`. Implementation in three follow-ups:

1. **Backend (`the-company`)** — accept the new field in the snapshot
   schema, write the merge. One handler change + two tests.
2. **Reporter (`juliuscaesar`)** — assemble the `runtime` block. New
   file `lib/company/runtime.py` for the introspection helpers, called
   by the existing reporter. Three tests.
3. **Frontend (`the-company`)** — render the block on `AgentDetail`.
   Pure UI work, no schema churn.

Independent reviews. Reporter can ship before frontend without harm —
the data lands in the JSON and waits. Backend should ship first or
concurrent.

## 9. Open questions

- **Q1 — `framework_commit` precision.** Today the framework_version
  string carries the *short* SHA (7 chars). For audit-grade drift
  detection a full 40-char SHA is better. Two options: (a) extend
  `framework_version()` to optionally include the full SHA;
  (b) the reporter does a `git rev-parse HEAD` and adds a separate
  `framework_commit_full` field. Default in v1: keep the short SHA;
  upgrade if anyone reports they need the long form.

- **Q2 — Renaming `deployment` to something cleaner.** "Deployment"
  is overloaded now that it mixes operator intent and runtime fact.
  A future rename to `topology` (operator-set) + `runtime` (reporter-set)
  as two siblings on the agent row would be cleaner. Not in scope; the
  nested-key trick keeps the rename optional.

- **Q3 — Push cadence for short-lived diagnostics.** If we ever need
  sub-30s freshness (debugging a crash loop in real time), the existing
  snapshot tick is too slow. Out of scope for v1; if it ever bites,
  add a separate `/runtime/heartbeat` endpoint with a faster cadence
  and a smaller payload.
