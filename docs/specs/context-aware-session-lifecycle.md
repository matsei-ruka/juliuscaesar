# Context-aware session lifecycle

**Status:** proposed
**Date:** 2026-06-06
**Scope:** specification only; no implementation in this PR
**Touches:** gateway sessions, heartbeat sessions, named workers, routing,
recovery, transcripts, brain adapters, Telegram commands, maintenance
scheduling, tool-output policy
**Related:** `docs/specs/parallel-slots.md`,
`docs/specs/telegram-slash-commands.md`,
`docs/specs/goal-integration.md`,
`docs/kb/subsystem/heartbeat-runner.md`

## 0. Summary

JuliusCaesar currently treats a provider-native session as permanent execution
memory. In gateway chat the mapping is:

```text
(channel, conversation_id, brain, slot) -> native session id
```

Each later event resumes that native session. The session therefore retains
user turns, assistant turns, tool calls, tool results, retries, and provider
metadata until it exceeds a model or account context limit.

That design must change. Provider sessions are bounded working memory, not
durable agent memory. JC must own continuity through transcripts, structured
checkpoints, goals, and L1/L2 memory, and it must rotate provider sessions
before they become unusable.

The target lifecycle is:

```text
measure -> route safely -> maintain while idle -> checkpoint -> rotate
```

with context exhaustion handled as a lifecycle event, never as a generic
transient retry.

This spec combines six controls:

1. Context telemetry stored per native session.
2. Capacity-aware model routing before dispatch.
3. Size-aware idle maintenance.
4. Framework-owned checkpoint and session rotation.
5. Bounded prompt and tool-result growth.
6. Context-specific recovery that rotates and retries at most once.

Size-gated routing to a larger-context model is an immediate safety valve. It
is not the terminal state: every resumed session must eventually rotate.

## 1. Incident class

The failure is deterministic:

1. A busy conversation repeatedly resumes one native session.
2. Code and operations work adds large tool results to that session.
3. The session grows beyond a cheaper model's usable context.
4. Triage still selects that cheaper model.
5. The provider rejects the request or attempts a paid/disabled extended
   context profile.
6. Generic recovery retries the same event against the same session.
7. Retry prompts are also written into the native session.
8. The session eventually fails for every model with `Prompt is too long`.

A fleet instance observed this progression in June 2026:

- the last successful request reported roughly 930K effective input tokens;
- individual retained tool results were 80-180 KB;
- later calls first failed because extended context required usage credits;
- subsequent calls failed with `Prompt is too long`;
- recovery classified the errors as unknown and retried the same session.

This is not an L1-size problem. It is unbounded native-session growth.

## 2. Current architecture constraints

The design must respect the current implementation:

1. **Native sessions are persisted in SQLite.**
   `lib/gateway/sessions.py` stores one `session_id` per
   `(channel, conversation_id, brain, slot)`.

2. **Brains are constructed per dispatch.**
   Lifecycle state cannot live on a `Brain` Python object. It must be persisted
   in SQLite or under `<instance>/state/gateway/`.

3. **Claude owns its resumed transcript.**
   `ClaudeBrain.needs_l1_preamble = False`; Claude Code auto-loads
   `CLAUDE.md` and `claude.sh` passes `--resume`.

4. **JC does not currently normalize usage for CLI brains.**
   `BrainResult` contains response and session information, but no common
   context-usage record. Some API adapters expose usage independently.

5. **The gateway does not consume the `/compact` signal.**
   The Telegram command creates `state/signals/compact`, but gateway mode has
   no code that acts on it.

6. **Recovery does not distinguish context exhaustion.**
   `Prompt is too long` and extended-context billing failures can reach the
   unknown retry path.

7. **Heartbeat sessions are separate.**
   Conversation-session maintenance belongs to the gateway. A normal
   heartbeat task cannot compact or rotate a chat session by resuming its own
   unrelated heartbeat session.

8. **JC cannot automatically intercept every native harness tool result.**
   It can bound context it builds itself. Bounding Claude Code's internal
   Bash/Read/tool results requires provider-specific hooks, wrappers, or
   harness instructions in addition to gateway changes.

9. **Heartbeat tasks resume independently by task name.**
   `lib/heartbeat/runner.py` stores
   `heartbeat/state/<task_name>.session`. A heartbeat cannot maintain a chat
   session, but its own recurring session can grow without bound.

10. **Named workers may resume prior runs.**
    `jc workers spawn --name <slug>` reuses the most recent captured
    `session_id` unless `--fresh` is set. Long-lived named worker lineages need
    the same pre-invoke guard and rotation policy.

## 3. Goals

### 3.1 Functional goals

- Prevent context-limit failures before dispatch when capacity is known.
- Preserve conversation continuity across native-session replacement.
- Keep normal routing economical while using larger-context models as a
  temporary bridge when appropriate.
- Perform expensive maintenance while idle when possible.
- Guarantee a synchronous hard backstop when no idle window occurs.
- Recover automatically from context exhaustion without retry amplification.
- Give `/compact` real, conversation-scoped behavior.
- Apply one lifecycle model across Claude, Codex, Gemini, Pi, and future
  brains, with provider-specific capability adapters.
- Apply the lifecycle to gateway chats, recurring heartbeat tasks, and named
  worker continuations.
- Keep parallel slots independent and safe.

### 3.2 Operational goals

- Expose current context pressure, session age, rotation count, and last
  checkpoint time in logs and diagnostics.
- Make context capacity explicit configuration, not an assumption derived
  from model nicknames.
- Avoid silently depending on paid 1M/extended-context availability.
- Preserve old native session files for audit; rotation removes only the
  active mapping.

## 4. Non-goals

- Infinite in-session recall.
- Replacing L1/L2 memory or conversation transcripts.
- Compacting on every topic change.
- Treating a larger model as permanent storage for an oversized session.
- Replaying the full historical transcript into every fresh session.
- Deleting provider-native session files during normal rotation.
- Solving provider billing or enabling paid overage automatically.
- Requiring every provider to support native compaction.

## 5. Principles

### 5.1 JC owns continuity

Durable continuity consists of:

- `state/transcripts/<conversation_id>.jsonl`;
- L1 and L2 memory;
- active goal/task state;
- a structured conversation checkpoint;
- a bounded tail of recent transcript turns.

The native session is disposable working memory.

### 5.2 Capacity is a profile, not a model-family guess

The framework must not encode assumptions such as "Sonnet is 200K" or "Opus
is 1M" without an explicit profile. Capacity can vary by provider, canonical
model id, CLI version, account entitlement, beta flag, and billing policy.

Every routed model must resolve to a context profile containing:

- canonical model identifier;
- usable input capacity;
- output reserve;
- whether extended context is enabled and paid;
- whether the profile can resume the current provider session;
- source of the value: built-in catalog, operator override, or provider
  discovery.

Unknown capacity uses conservative behavior and turn/age fallbacks.

### 5.3 Routing buys time; rotation bounds growth

If the triage-selected model cannot safely accept the current session, JC may
route the turn to a compatible larger-context model. That preserves service
while the session remains below its rotation threshold.

It does not remove the need to rotate. A session above the hard rotation
threshold is checkpointed and replaced before normal dispatch.

### 5.4 Topic change is a hint, not an independent trigger

Operations conversations naturally move between incidents and later return to
them. Topic classification is probabilistic and mid-conversation compaction
adds latency.

A topic-change signal may make an already-eligible session a better rotation
candidate:

```text
topic changed AND context above soft threshold -> rotate sooner
```

Topic change alone must not compact or rotate a healthy session.

## 6. Terminology

**Native session**
: A provider/harness session identified by a gateway row, heartbeat session
file, or worker lineage record.

**Session owner**
: The framework execution identity that owns a resumable native session. It is
one gateway slot, one heartbeat task, or one named worker lineage.

**Context profile**
: Capacity and entitlement metadata for a canonical routed model.

**Routing pressure**
: Estimated required context divided by the triage-selected model profile's
usable capacity. Determines whether that model can accept the next turn.

**Lifecycle pressure**
: Estimated occupied context divided by the session ceiling: the largest
explicitly enabled, session-compatible context profile that JC is allowed to
use for this brain. Determines when the session must be maintained or rotated.

**Checkpoint**
: Framework-owned structured state sufficient to continue in a fresh native
session without replaying the full transcript.

**Rotation**
: Archive the active native-session mapping and dispatch the next turn without
`--resume`, seeding it with checkpoint and recent context.

**Native compaction**
: A provider-specific operation that reduces an existing native session while
keeping its id.

**Idle maintenance**
: Context maintenance performed after a conversation has had no inbound or
in-flight activity for a configured period.

## 7. Execution domains

The lifecycle engine is shared, but each runner supplies an owner adapter.

### 7.1 Gateway conversation

Owner key:

```text
gateway:<channel>:<conversation_id>:<brain>:<slot>
```

Continuity sources:

- conversation checkpoint;
- gateway transcript;
- active goal;
- L1/L2 memory;
- recent transcript tail.

Maintenance:

- pre-dispatch guard on every event;
- idle scanner;
- `/compact`;
- context-specific recovery.

### 7.2 Heartbeat task

Owner key:

```text
heartbeat:<task_name>:<tool>
```

Continuity sources:

- prior task checkpoint;
- recent task outputs and bundles by path;
- task definition;
- L1/L2 memory.

Maintenance:

- pre-run guard before setting `JC_RESUME_SESSION`;
- post-run usage capture;
- checkpoint and rotation between runs;
- no use of the chat `/compact` command.

Heartbeat already invokes L1 and task context on every run. Its checkpoint
must not duplicate full prior bundles or outputs; it records only durable task
state needed by the next run.

### 7.3 Named worker lineage

Owner key:

```text
worker:<name>:<brain>
```

Continuity sources:

- worker-lineage checkpoint;
- prior worker result;
- current worker brief;
- referenced artifacts and repository state.

Maintenance:

- pre-spawn guard before reusing the prior named worker's `session_id`;
- post-run usage capture;
- rotation means spawn without the prior session and seed the brief with the
  lineage checkpoint;
- anonymous and `--fresh` workers have no resumable owner and need no
  lifecycle record after completion.

### 7.4 Shared owner contract

Each runner must provide:

```python
class SessionOwnerAdapter(Protocol):
    def owner_key(self) -> str: ...
    def load_native_session_id(self) -> str | None: ...
    def clear_native_session_id(self, expected: str | None) -> bool: ...
    def continuity_sources(self) -> list[str]: ...
    def is_idle(self) -> bool: ...
```

The shared lifecycle service must not hard-code gateway SQLite assumptions.

## 8. Context telemetry

### 8.1 Normalized record

Each successful invocation should produce a normalized usage record:

```python
@dataclass(frozen=True)
class ContextUsage:
    input_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    output_tokens: int | None
    effective_input_tokens: int | None
    source: str                 # api | native_session | estimate
    measured_at: str
```

For Anthropic-style usage:

```text
effective_input_tokens =
    input_tokens
  + cache_creation_input_tokens
  + cache_read_input_tokens
```

The provider adapter owns this calculation because usage semantics differ.
The router consumes only `effective_input_tokens`.

### 8.2 Collection order

Preferred sources:

1. Structured usage returned directly by the adapter/API.
2. Provider-native session metadata read after invocation.
3. Conservative local estimate from prompt bytes/tokens and prior usage.
4. Turn-count/session-age policy when no token estimate is available.

For Claude CLI, reading the newest successful assistant usage entry from the
known active session JSONL is an acceptable first implementation. It is a
provider adapter detail, not routing logic. Results must be persisted after
the call so the hot routing path does not scan a multi-megabyte JSONL before
every dispatch.

Failed synthetic provider messages with zero usage must not overwrite the last
known good measurement.

### 8.3 Session metadata

Extend the logical session record with:

```text
last_model
context_profile
effective_input_tokens
usage_source
turn_count
rotation_count
last_checkpoint_at
last_activity_at
maintenance_state
```

Exact storage may be additional `sessions` columns or a companion table. A
companion `session_lifecycle` table is preferable if migration complexity or
provider-specific fields would make `sessions` unstable.

Telemetry is keyed by the session owner key. Gateway storage may use its
existing tuple directly; heartbeat and workers use their task/name identity.

## 9. Context profiles and configuration

Proposed configuration:

```yaml
session_lifecycle:
  enabled: true

  thresholds:
    observe_ratio: 0.50
    idle_maintenance_ratio: 0.60
    rotate_ratio: 0.70
    emergency_ratio: 0.85

  reserves:
    output_tokens: 16000
    turn_input_tokens: 12000

  idle:
    after_seconds: 600
    scan_interval_seconds: 60

  checkpoint:
    recent_transcript_messages: 16
    max_bytes: 24000
    refresh_ratio: 0.50
    max_age_seconds: 3600

  fallback_limits:
    max_turns: 100
    max_age_hours: 24

  tool_results:
    max_inline_bytes: 20000
    max_inline_lines: 500

  native_compaction:
    enabled: true
    fallback_to_rotation: true

  model_profiles:
    claude-sonnet-4-6-standard:
      model: claude-sonnet-4-6
      variant: standard
      input_capacity_tokens: 200000
      extended_context: false
      enabled: true
      allow_capacity_upgrade: true
    claude-opus-4-8-standard:
      model: claude-opus-4-8
      variant: standard
      input_capacity_tokens: 200000
      extended_context: false
      enabled: true
      allow_capacity_upgrade: true
    claude-opus-4-8-extended:
      model: claude-opus-4-8
      variant: extended
      input_capacity_tokens: 1000000
      extended_context: true
      enabled: false
      allow_capacity_upgrade: true
```

The concrete model values above are examples, not normative defaults.

Validation requirements:

- ratios must be strictly increasing and below `1.0`;
- reserves must be positive;
- an extended-context profile cannot contribute to the session ceiling unless
  `enabled` is explicitly true;
- a profile cannot be selected to replace the triage choice unless
  `allow_capacity_upgrade` is true;
- the adapter must resolve the actual standard/extended variant used for an
  invocation; a bare model alias is not sufficient evidence that extended
  context is available;
- unknown model ids may route normally while context is small, but cannot be
  used as a claimed high-capacity escape route.

`jc-upgrade` must preserve this operator-owned block when implementation lands.

## 10. Pre-dispatch guard

The guard runs after normal brain/model selection and session lookup, but
before adapter spawn.

It computes two independent values:

```text
routing_pressure =
    required_context / selected_profile.input_capacity_tokens

lifecycle_pressure =
    current_context / session_ceiling.input_capacity_tokens
```

The session ceiling is the largest explicitly enabled profile that the
provider says can resume this native session. If no larger profile is enabled,
the selected/default standard profile is the ceiling.

Example:

- a 180K session routed to a 200K model has high routing pressure and should
  not be sent to that model;
- if an entitled 1M profile is explicitly enabled and resume-compatible, the
  same session has only 18% lifecycle pressure and may temporarily route to
  that profile;
- if no such profile is enabled, the 200K profile is the session ceiling and
  the session should already have rotated before reaching 180K.

### 10.1 Required estimate

```text
required_context =
    last_effective_input
  + estimated_new_prompt
  + configured_turn_input_reserve
  + configured_output_reserve
```

The estimate deliberately includes headroom for tool use and response output.
It must not wait until `current_context == model_limit`.

### 10.2 Decision table

| Condition | Action |
|---|---|
| no resumed session | use selected model; seed checkpoint if one exists |
| lifecycle pressure at or above emergency ratio | skip native compaction; rotate synchronously |
| lifecycle pressure at or above rotate ratio | checkpoint and rotate before dispatch |
| selected profile safely fits | dispatch normally |
| selected profile does not fit; enabled compatible larger profile safely fits | temporarily upgrade model |
| usage unknown but turn/age limit exceeded | checkpoint and rotate |
| no safe profile and rotation unavailable | fail visibly; do not invoke provider |

Temporary model upgrade must emit a distinct routing reason:

```text
reason=context_capacity_upgrade
```

It must not create sticky model routing. The next event runs normal triage
again against updated lifecycle state.

### 10.3 Profile compatibility

The guard must ask the provider adapter whether a larger model can resume the
same native session. If not, model upgrade becomes rotation:

```text
selected profile cannot fit
+ larger profile cannot resume this session
-> checkpoint and rotate onto larger profile
```

This avoids relying on undocumented cross-model resume behavior.

## 11. Checkpoint format

Checkpoint path:

```text
state/session-lifecycle/checkpoints/<owner-key-hash>.json
```

Suggested schema:

```json
{
  "version": 1,
  "owner_key": "gateway:telegram:223588914:claude:0",
  "owner_kind": "gateway",
  "channel": "telegram",
  "conversation_id": "223588914",
  "brain": "claude",
  "slot": 0,
  "created_at": "2026-06-06T10:00:00Z",
  "source_session_id": "70623260-...",
  "active_goal": "Ship the context lifecycle specification",
  "open_threads": [
    {
      "topic": "session growth",
      "status": "in_progress",
      "next_action": "open specification PR"
    }
  ],
  "decisions": [
    "Provider sessions are disposable working memory",
    "Topic change alone does not trigger compaction"
  ],
  "completed_work": [],
  "pending_user_requests": [],
  "constraints": [],
  "references": {
    "files": [],
    "prs": [],
    "hosts": [],
    "incident_ids": []
  },
  "unresolved_questions": [],
  "recent_transcript_cursor": {
    "last_timestamp": "2026-06-06T09:59:00Z",
    "last_message_id": null
  }
}
```

Requirements:

- atomic tempfile + replace write;
- bounded serialized size;
- deterministic schema validation;
- no secrets copied from tool output;
- no claim that the checkpoint is the full transcript;
- preserve open work, decisions, user commitments, and operational references;
- retain the previous checkpoint until the replacement is valid.

Checkpoint generation may use a model, but lifecycle correctness cannot depend
on the oversized native session being callable. The implementation therefore
needs two modes:

1. **Normal checkpoint:** summarize while the current session/profile is still
   usable.
2. **Emergency checkpoint:** synthesize from the durable transcript tail,
   current goal, prior checkpoint, and owner metadata using a fresh
   stateless or new-session call.

Checkpoint freshness is proactive. At or above `checkpoint.refresh_ratio`, an
owner's maintenance boundary should refresh a stale checkpoint even if it does
not compact or rotate yet. For gateway conversations that boundary is an idle
scan; for heartbeat and workers it is the end/start of a run. This keeps
emergency recovery from depending on summarizing a nearly exhausted session.

## 12. Rotation

Rotation is the primary hard-bound mechanism.

### 12.1 Normal flow

```text
acquire lifecycle lock for session owner
-> confirm no other rotation won the race
-> write or refresh checkpoint
-> append old session id to rotation history
-> clear active session mapping
-> build handoff prompt
-> invoke without --resume
-> capture and persist new session id
-> persist new telemetry
-> release lock
```

The handoff prompt contains:

- the structured checkpoint;
- active goal/task anchor;
- a bounded transcript tail;
- a statement that older detail is available through transcript/memory tools.

L1 continues to arrive through the brain's normal mechanism. It must not be
duplicated inside the checkpoint.

The current user message remains the normal event body and appears exactly
once. The handoff context is prepended around it; it does not embed a duplicate
copy.

### 12.2 Rotation history

Persist an audit record:

```text
old_session_id
new_session_id
reason
context_before
profile_before
checkpoint_path
rotated_at
```

Old provider-native files are not deleted.

### 12.3 Parallel slots

Lifecycle locks and checkpoints are slot-specific. Rotating slot 1 must not
clear slot 0.

The dispatcher must re-check session identity after acquiring the lock because
another in-flight event may have already rotated or updated that slot.

### 12.4 Failure semantics

- Checkpoint failure below emergency threshold: retain old session and surface
  an operational error; do not clear the mapping.
- Emergency checkpoint failure: rotate using prior checkpoint + bounded
  transcript tail, and log degraded continuity.
- Fresh-session capture failure: leave the active mapping empty so the next
  call starts fresh; preserve rotation audit and checkpoint.
- Handoff retry: at most once for the same event.

## 13. Native compaction

Native compaction is optional optimization, not the portability layer.

Use it only when:

- provider adapter declares support;
- session is idle;
- lifecycle pressure is above `idle_maintenance_ratio` and below
  `emergency_ratio`;
- no invocation is in flight for the owner.

After compaction, JC must re-measure context. Success requires a meaningful
reduction. If the provider operation fails, is unsupported, or leaves
lifecycle pressure above `rotate_ratio`, fall back to framework rotation.

The provider adapter interface should expose capabilities rather than routing
code checking brain names:

```python
class SessionLifecycleCapabilities:
    reports_usage: bool
    supports_native_compaction: bool
    supports_cross_model_resume: bool
```

For providers without native compaction, idle maintenance rotates directly.

## 14. Idle maintenance

Gateway idle maintenance is owned by the gateway process or a gateway-aware
supervisor job. It is not a regular heartbeat brain task.

Eligibility:

```text
no inbound event for idle.after_seconds
AND no event running/claimed for the slot
AND lifecycle pressure >= idle_maintenance_ratio
```

Maintenance action:

1. Prefer native compaction when supported.
2. Otherwise checkpoint and rotate.
3. Re-measure and record result.

Busy conversations may never become idle. Therefore the pre-dispatch
`rotate_ratio` remains mandatory.

The scanner must use a lease/lock so multiple gateway processes cannot
maintain the same slot concurrently.

Heartbeat and named workers need no independent idle scanner. Their natural
run boundary is the maintenance window: measure after a run, then checkpoint
or clear the resume mapping before the next scheduled/spawned run when policy
requires it.

## 15. Topic-change signal

Topic affinity already exists for parallel-slot routing. A future lifecycle
implementation may consume that signal, but only as a modifier:

```text
if topic_changed and lifecycle_pressure >= idle_maintenance_ratio:
    lower the preferred maintenance deadline
```

It must not:

- compact immediately on every classifier verdict;
- block the current user turn while lifecycle pressure is below the hard
  threshold;
- discard checkpoint threads from earlier topics;
- replace deterministic size/age policy.

For truly concurrent topics, parallel slots or explicit task conversations are
the correct isolation mechanism.

## 16. Tool-result and prompt growth controls

Rotation guarantees eventual boundedness, but growth rate still matters.

### 16.1 Framework-controlled context

JC must bound:

- transcript priming blocks;
- global parallel-slot context;
- checkpoints;
- pre-fetch bundles and other runner-built adapter prompts;
- recovery diagnostics inserted into prompts;
- any tool result returned through JC-owned MCP servers or wrappers.

When an artifact exceeds inline limits:

```text
save full artifact
-> return summary/excerpt + path + byte/line counts
-> allow explicit ranged follow-up reads
```

### 16.2 Native harness tools

Claude Code and other native CLIs may execute tools internally, outside the
gateway's stdin/stdout boundary. JC cannot truncate those results in
`Brain.invoke` after they have already entered the provider session.

Provider-specific implementation options, in preference order:

1. supported tool-result hooks that replace oversized output with an artifact
   reference;
2. JC-owned MCP/tool wrappers with byte and line limits;
3. shell wrappers for high-risk commands;
4. system-prompt policy requiring targeted reads (`rg`, ranged `sed`, log
   tails, SQL `LIMIT`) and forbidding full dumps by default.

The implementation must document which native tools are enforceable and which
are policy-only. It must not claim universal hard truncation without a real
interception point.

### 16.3 Retry amplification

Recovery must not append the same full prompt repeatedly to a poisoned native
session. Context-lifecycle errors use the dedicated recovery path in section
17.

## 17. Context-specific recovery

Add recovery classifications:

```text
context_exhausted
context_profile_unavailable
```

Deterministic signatures include:

- `Prompt is too long`;
- `context window exceeded`;
- `maximum context length`;
- provider-equivalent token-limit errors;
- extended/1M context requires credits or entitlement.

`context_profile_unavailable` is distinct because the session may still fit a
standard profile after rotation. It is not an authentication failure and must
not mark the entire brain failed.

Recovery behavior:

```text
context_exhausted
-> do not generic-retry
-> checkpoint from durable state if needed
-> rotate active slot
-> re-enqueue once with context_rotation_recovery=true

context_profile_unavailable
-> if current context safely fits an enabled standard profile, route there
-> otherwise rotate to an enabled standard profile
-> re-enqueue once
```

If the recovery marker is already present and the fresh session fails with the
same class, fail visibly and alert. Never loop.

The old session mapping must be cleared with the same race-safe semantics as
`session_missing`, extended to include slot for gateway owners. Heartbeat and
worker runners must apply the same expected-session compare-and-clear rule to
their own owner stores.

## 18. `/compact` behavior

`/compact` becomes an explicit lifecycle operation for the originating
conversation.

Required behavior:

1. Resolve `(channel, conversation_id)` from command metadata.
2. Inspect active brain slots for that conversation.
3. For each eligible slot, request maintenance.
4. Prefer native compaction when safe; otherwise checkpoint and rotate.
5. Report measured before/after state.

Example response:

```text
Context maintenance complete.
claude slot 0: 142K -> 31K tokens, rotated.
Checkpoint: current goals, 3 open threads, 5 decisions preserved.
```

The command must not write a global unscoped signal. If maintenance is queued
because a slot is busy, the response must say so and the gateway must have a
consumer that executes it later.

## 19. Observability

Structured log kinds:

```text
context_usage_updated
context_capacity_upgrade
context_idle_maintenance
context_native_compaction
context_checkpoint_written
context_session_rotated
context_recovery
context_recovery_failed
context_tool_result_externalized
```

Each lifecycle log should include:

```text
owner kind and owner key
brain
channel, conversation_id, and slot when applicable
session id prefix
model/profile
effective input tokens
selected-model capacity and routing pressure
session ceiling and lifecycle pressure
reason
```

`jc doctor` or a dedicated `jc sessions` view should show:

- active gateway, heartbeat, and named-worker sessions ordered by lifecycle
  pressure;
- sessions with unknown telemetry;
- age and turn count;
- last checkpoint and rotation;
- pending maintenance;
- recent context-recovery failures.

Do not expose transcript content in diagnostics by default.

## 20. State machine

```text
HEALTHY
  lifecycle pressure < observe_ratio

OBSERVED
  lifecycle pressure >= observe_ratio
  telemetry and checkpoint freshness monitored

MAINTENANCE_ELIGIBLE
  lifecycle pressure >= idle_maintenance_ratio
  compact/rotate while idle

ROTATE_REQUIRED
  lifecycle pressure >= rotate_ratio
  rotate before next normal dispatch

EMERGENCY
  lifecycle pressure >= emergency_ratio or provider rejects context
  skip native compaction; emergency checkpoint + rotate

RECOVERING
  one fresh-session redispatch allowed

FAILED
  fresh session also failed; surface and alert
```

Transitions are based on persisted telemetry and re-evaluated under the
lifecycle lock.

## 21. Suggested implementation phases

This PR contains no implementation. A later implementation should be split so
the urgent guard does not wait for every optimization.

### Phase 1: measurement and no-amplification recovery

- normalized usage record;
- persisted lifecycle telemetry;
- deterministic context-error classification;
- stop generic retries for context errors;
- diagnostics/logging.
- shared session-owner identity for gateway, heartbeat, and workers.

### Phase 2: capacity-aware routing

- model context profiles;
- pre-dispatch fit calculation;
- temporary larger-profile routing;
- conservative unknown-capacity behavior.

### Phase 3: framework checkpoint and rotation

- checkpoint schema/store;
- handoff prompt;
- slot-safe rotation and audit history;
- one-shot recovery onto a fresh session.

### Phase 4: idle maintenance and `/compact`

- gateway maintenance scanner;
- provider native-compaction capability;
- real conversation-scoped Telegram command;
- rotation fallback.

### Phase 5: growth-rate controls

- framework prompt caps;
- artifact externalization;
- provider-specific native tool hooks/wrappers;
- policy reporting for unenforceable tools.

## 22. Test plan

### 22.1 Unit

- Routing and lifecycle pressure calculations include input, cache creation,
  cache read, and reserves.
- Failed zero-usage records do not replace last known good telemetry.
- Ratio validation rejects unordered thresholds.
- Capacity guard decisions cover fit, upgrade, rotate, emergency, and unknown
  usage.
- Topic change alone does not rotate.
- Checkpoint schema and maximum size validation.
- Context errors classify deterministically without LLM recovery triage.
- Recovery marker prevents a second rotation loop.
- Rotation clears only the requested slot.

### 22.2 Integration

- Grow a fake resumed session past the selected model profile; assert a larger
  compatible profile is selected.
- Grow past `rotate_ratio`; assert adapter is invoked without the old resume id
  and receives checkpoint handoff.
- Simulate `Prompt is too long`; assert no generic backoff retry occurs and one
  fresh-session redispatch is created.
- Simulate extended-context entitlement failure; assert standard-profile
  rotation rather than global brain failure.
- Run two parallel slots; rotate one while the other remains resumable.
- Issue `/compact` during idle; assert maintenance runs for the originating
  conversation only.
- Issue `/compact` during an in-flight turn; assert queued maintenance executes
  after the slot becomes idle.
- Fail native compaction; assert rotation fallback.
- Fail checkpoint generation near emergency; assert degraded checkpoint from
  durable state.
- Grow a recurring heartbeat task; assert its next run rotates without
  touching any chat session.
- Grow a named worker lineage; assert the next spawn uses the lineage
  checkpoint without the old session id.

### 22.3 Soak

- Multi-day synthetic conversation with large tool artifacts.
- Assert no active session crosses `emergency_ratio`.
- Assert response continuity survives multiple rotations.
- Assert native session files and gateway transcript remain available for
  audit.
- Assert token/cost use does not regress from unnecessary idle compaction.

## 23. Rollout and compatibility

- Feature is config-gated during first release.
- Existing gateway rows, heartbeat session files, and named-worker session ids
  start with unknown telemetry and become measured on their next successful
  call.
- Sessions already above a hard threshold rotate on their next event.
- Existing native session files remain untouched.
- Missing checkpoints fall back to transcript tail and L1/L2 retrieval.
- Brains without usage reporting use turn and age limits until an adapter
  gains telemetry.
- Brains without native compaction rotate.
- `session_lifecycle.enabled: false` preserves current behavior, except
  context-limit errors should still stop generic retry amplification.

## 24. Acceptance criteria

The implementation is complete only when:

1. No active gateway, recurring heartbeat, or named-worker session can grow
   indefinitely without a lifecycle decision.
2. The router cannot dispatch a resumed session to a model profile that is
   known not to fit.
3. Larger-context routing is temporary and does not suppress hard rotation.
4. A context-limit error does not retry the same poisoned session.
5. Rotation preserves active goals, open threads, decisions, pending requests,
   references, and recent dialogue through a bounded checkpoint.
6. Busy conversations rotate synchronously even if no idle window occurs.
7. `/compact` performs or queues real scoped maintenance.
8. Parallel slots rotate independently.
9. Observability shows context pressure and lifecycle actions without reading
   provider files manually.
10. Tool-output limits accurately distinguish hard enforcement from
    prompt-policy guidance.

## 25. Rejected alternatives

### Force Opus whenever context exceeds 180K

Useful short-term guard, rejected as the complete solution. It assumes a
specific capacity/entitlement and allows unbounded growth toward the larger
limit.

### Compact only when idle

Rejected as the only trigger. A busy conversation may never become idle.

### Compact on every topic change

Rejected. Classification is fuzzy, conversations return to prior topics, and
the trigger adds latency without addressing the actual bounded-resource
condition.

### Reset sessions without a checkpoint

Rejected as the default. It avoids failure but causes preventable continuity
loss. It remains an emergency fallback when checkpoint generation fails.

### Treat transcripts as prompts and replay all history

Rejected. This recreates the same unbounded-context problem under framework
control.

### Depend exclusively on provider-native compaction

Rejected. It is provider-specific, may fail when already oversized, and does
not give JC a portable continuity contract.
