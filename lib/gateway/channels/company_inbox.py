"""Company-inbox channel — pulls task-graph assignments from the-company.

Polls ``GET /api/agents/{self}/inbox`` every ``poll_interval_seconds`` and
injects each new task as a synthetic ``company.task_assigned`` event into the
local ``queue.db``. The brain receives it through the normal dispatch path.

Inbound-only: ``send()`` is a no-op (the brain acts on tasks via its own HTTP
skill — out of scope here). Credentials and agent identity are the *same ones
the supervisor reporter uses* — ``COMPANY_*`` from ``.env`` loaded by
``lib/company/conf.py`` into a ``CompanyConfig`` and driven through
``lib/company/client.py``.

Dedup is belt-and-suspenders: a boot-scoped in-memory ``seen`` set, plus the
``queue.db`` partial unique index ``idx_events_dedup (source,
source_message_id)`` — ``queue.enqueue`` is ``INSERT OR IGNORE`` so a repeat is
a true no-op.

Affinity: sub-tasks of one task tree share ``conversation_id =
task-root:<root_id>`` so the gateway's parallel-slot relatedness classifier
groups/routes them together. A NULL conversation_id would run each task alone
on slot 0 with no affinity, so the key is always set.

Spec: docs/specs/company-inbox-channel.md.
"""

from __future__ import annotations

import dataclasses
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import ChannelConfig
from .base import EnqueueFn, LogFn

try:  # Company integration is optional (requires `requests` on the path).
    from company import conf as company_conf  # type: ignore
    from company.client import CompanyClient, CompanyError  # type: ignore
except Exception:  # noqa: BLE001
    company_conf = None  # type: ignore
    CompanyClient = None  # type: ignore

    class CompanyError(Exception):  # type: ignore
        status = 0


DEFAULT_POLL_INTERVAL_SECONDS = 10
DEFAULT_MAX_NEW_PER_TICK = 5
DEFAULT_STATUS_FILTER = ("pending", "accepted")
BACKOFF_CAP_SECONDS = 300
DEGRADED_MULTIPLIER = 4
WARN_AFTER_CONSECUTIVE_FAILURES = 3
# When emit_task_closed is on, poll these non-terminal statuses *in addition*
# to the inject filter, so a previously-injected task disappears from the
# polled set only when it goes terminal — not merely on a pending→in_progress
# move. Requires the-company to honour these status values in the inbox query.
CLOSURE_EXTRA_STATUSES = ("in_progress", "blocked")
CLOSURE_POLL_LIMIT = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CompanyInboxChannel:
    name = "company-inbox"

    def __init__(self, instance_dir: Path, cfg: ChannelConfig, log: LogFn):
        self.instance_dir = Path(instance_dir)
        self.cfg = cfg
        self.log = log
        self.poll_interval = max(
            1, int(getattr(cfg, "poll_interval_seconds", None) or DEFAULT_POLL_INTERVAL_SECONDS)
        )
        self.max_new_per_tick = max(
            1, int(getattr(cfg, "max_new_per_tick", None) or DEFAULT_MAX_NEW_PER_TICK)
        )
        self.status_filter = tuple(
            getattr(cfg, "inbox_status_filter", ()) or DEFAULT_STATUS_FILTER
        )
        # Opt-in precise clear signal (§5.3 A). Default off → goal clear relies
        # on the goal_cache TTL floor (§5.3 B), which needs no backend support.
        self.emit_task_closed = bool(getattr(cfg, "emit_task_closed", False))
        # Wider poll set used only for closure detection (superset of inject set).
        self.active_filter = tuple(
            dict.fromkeys((*self.status_filter, *CLOSURE_EXTRA_STATUSES))
        )

        # Boot-scoped dedup. Wiped on restart; the queue.db unique index keeps
        # a re-inject a no-op (see module docstring).
        self._seen: set[str] = set()
        # task_id -> conversation_id for injected (assigned) tasks, used to
        # detect closure when emit_task_closed is on.
        self._injected: dict[str, str] = {}
        self._company_cfg: Any = None
        self._client: Any = None

        # In-memory health/diagnostics (watchdog-readable via health()).
        self._lock = threading.Lock()
        self._auth_failing = False
        self._degraded = False
        self._escalated = False
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._last_injected_at: str | None = None
        # Discovery state — set to True once /api/agents/me has populated
        # COMPANY_AGENT_ID (or it was already present at boot).
        self._agent_id_discovered: bool = False
        self._discovery_escalated: bool = False

    # ── Channel lifecycle ────────────────────────────────────────────────

    def ready(self) -> bool:
        if company_conf is None or CompanyClient is None:
            self.log(
                "company-inbox disabled: company package unavailable",
                kind="company_inbox_disabled",
            )
            return False
        if not company_conf.is_enabled(self.instance_dir):
            self.log(
                "company-inbox disabled: company integration not configured "
                "(need COMPANY_ENDPOINT + api_key/enrollment_token + company.enabled)",
                kind="company_inbox_disabled",
            )
            return False
        return True

    def run(self, enqueue: EnqueueFn, should_stop: Callable[[], bool]) -> None:
        if not self.ready():
            return
        self._load_client()

        # Boot-time discovery: if .env did not persist COMPANY_AGENT_ID
        # (every agent registered before PR #64) we ask the-company for
        # our own UUID and persist it. Without this we'd fall back to
        # instance_id (SHA256) and silently 400 forever on /inbox.
        # Spec: docs/specs/agent-self-discovery.md §4.1.
        if not self._cfg_agent_id():
            if not self._discover_agent_id(should_stop):
                # Discovery never succeeded before shutdown — exit cleanly.
                self._close_client()
                self.log("company-inbox stopped", kind="company_inbox_stop")
                return

        self.log(
            f"company-inbox polling every {self.poll_interval}s "
            f"statuses={','.join(self.status_filter)} agent={self._agent_id()}",
            kind="company_inbox_start",
        )

        interval: float = float(self.poll_interval)
        while not should_stop():
            try:
                self._poll_once(enqueue)
                self._on_success()
                interval = float(self.poll_interval)
            except CompanyError as exc:
                interval = self._handle_error(exc)
            except Exception as exc:  # noqa: BLE001 — never let a tick crash the thread
                with self._lock:
                    self._consecutive_failures += 1
                    self._last_error = str(exc)
                    failures = self._consecutive_failures
                if failures == WARN_AFTER_CONSECUTIVE_FAILURES:
                    self.log(
                        f"company-inbox error (x{failures}): {exc}",
                        kind="company_inbox_error",
                    )
                interval = min(BACKOFF_CAP_SECONDS, max(float(self.poll_interval), interval) * 2)
            self._sleep(interval, should_stop)

        self._close_client()
        self.log("company-inbox stopped", kind="company_inbox_stop")

    def send(self, response: str, meta: dict[str, Any]) -> str | None:
        return None

    def close(self) -> None:
        self._close_client()

    # ── Polling ──────────────────────────────────────────────────────────

    def _poll_once(self, enqueue: EnqueueFn) -> int:
        agent_id = self._agent_id()
        # When closure detection is on we poll the wider non-terminal set (so a
        # task disappears only on going terminal) with a larger limit; otherwise
        # just the inject filter.
        statuses = self.active_filter if self.emit_task_closed else self.status_filter
        limit = CLOSURE_POLL_LIMIT if self.emit_task_closed else self.max_new_per_tick * 2
        result = self._client.get_inbox(agent_id=agent_id, statuses=statuses, limit=limit)
        items = _extract_items(result)
        # Defensive created_at ASC: the server is asked to order, but a flooded
        # inbox is stretched over ticks (§6) so oldest-first must hold locally.
        items.sort(key=lambda t: str(t.get("created_at") or ""))

        present_ids: set[str] = set()
        injected = 0
        ids: list[str] = []
        for task in items:
            task_id = task.get("id") if task.get("id") is not None else task.get("task_id")
            if task_id is None:
                continue
            task_id = str(task_id)
            owner = task.get("owner_agent_id")
            if owner is not None and str(owner) != str(agent_id):
                # §8.5 — must never happen (server filters by owner). If a
                # backend bug leaks another agent's task, skip; do not inject.
                self.log(
                    f"company-inbox ownership mismatch task={task_id} "
                    f"owner={owner} self={agent_id}; skipping",
                    kind="company_inbox_ownership_mismatch",
                )
                continue
            present_ids.add(task_id)
            status = str(task.get("status") or "")
            # Inject only tasks in the inject filter (pending/accepted). Tasks in
            # the closure-only statuses (in_progress/blocked) are tracked for
            # presence but not re-injected. A missing status is treated as
            # injectable (defensive against a backend that omits it).
            injectable = (not self.status_filter) or status == "" or status in self.status_filter
            if not injectable:
                continue
            if injected >= self.max_new_per_tick:
                continue  # cap injections; keep scanning so present_ids is complete
            if task_id in self._seen:
                continue
            self._inject(enqueue, task, task_id)
            self._seen.add(task_id)
            injected += 1
            ids.append(task_id)

        if injected:
            with self._lock:
                self._last_injected_at = _now_iso()
            self.log(
                f"company-inbox tick injected={injected} task_ids={ids} "
                f"cache_size={len(self._seen)}",
                kind="company_inbox_tick",
            )

        if self.emit_task_closed:
            self._detect_closures(enqueue, present_ids, truncated=len(items) >= limit)

        return injected

    def _detect_closures(
        self, enqueue: EnqueueFn, present_ids: set[str], *, truncated: bool
    ) -> None:
        """Emit company.task_closed for injected tasks that left the active set.

        Skipped when the poll was truncated: absence could be truncation rather
        than terminal, and a false close would prematurely clear a goal.
        """
        if truncated:
            return
        for task_id in [tid for tid in self._injected if tid not in present_ids]:
            conversation_id = self._injected.pop(task_id, None)
            self._seen.discard(task_id)
            if not conversation_id:
                continue
            root_id = (
                conversation_id.split("task-root:", 1)[-1]
                if conversation_id.startswith("task-root:")
                else conversation_id
            )
            enqueue(
                source=self.name,
                source_message_id=f"task-closed:{task_id}",
                user_id=None,
                conversation_id=conversation_id,
                content=f"Task {task_id} closed.",
                meta={"task_id": task_id, "root_id": root_id, "kind": "task_closed"},
            )
            self.log(
                f"company-inbox task closed task={task_id} conv={conversation_id}",
                kind="company_inbox_task_closed",
            )

    def _inject(self, enqueue: EnqueueFn, task: dict[str, Any], task_id: str) -> None:
        root_id = task.get("root_id") or task_id
        title = str(task.get("title") or "").strip()
        description = str(task.get("description") or "").strip()
        content = "\n\n".join(part for part in (title, description) if part)
        if not content:
            content = f"Task {task_id} assigned (no title/description)."

        created_by = task.get("created_by")
        user_id: str | None = None
        if isinstance(created_by, dict) and created_by.get("id") is not None:
            user_id = str(created_by["id"]) or None

        meta: dict[str, Any] = {
            "task_id": task_id,
            "root_id": root_id,
            "parent_id": task.get("parent_id"),
            "company_id": task.get("company_id"),
            "created_by": created_by,
            "deadline_at": task.get("deadline_at"),
            "max_nodes": task.get("max_nodes"),
            "max_depth": task.get("max_depth"),
            "max_age_secs": task.get("max_age_secs"),
            "payload": task.get("payload"),
            "company_status": task.get("status"),
            # title/description carried so the goal anchor (PR #65) can format
            # the goal text from meta without re-parsing content.
            "title": title,
            "description": description,
            "kind": "task_assigned",
        }

        conversation_id = f"task-root:{root_id}"
        enqueue(
            source=self.name,
            source_message_id=f"task:{task_id}",
            user_id=user_id,
            conversation_id=conversation_id,
            content=content,
            meta=meta,
        )
        self._injected[task_id] = conversation_id

    # ── Error handling ───────────────────────────────────────────────────

    def _on_success(self) -> None:
        with self._lock:
            recovered = self._degraded or self._consecutive_failures
            self._consecutive_failures = 0
            self._degraded = False
            self._escalated = False
            self._auth_failing = False
            self._last_error = None
        if recovered:
            self.log("company-inbox recovered", kind="company_inbox_recovered")

    def _handle_error(self, exc: "CompanyError") -> float:
        status = getattr(exc, "status", 0)
        with self._lock:
            self._consecutive_failures += 1
            self._last_error = f"status={status}: {exc}"
            failures = self._consecutive_failures

        if status == 401:
            return self._handle_auth_failure()

        # Transient: transport (status 0), 5xx, 429. Warn once at the 3rd
        # failure, then stay silent until success (§8.1).
        if failures == WARN_AFTER_CONSECUTIVE_FAILURES:
            self.log(
                f"company-inbox backend error (x{failures}) status={status}: {exc}",
                kind="company_inbox_error",
            )
        # Exponential backoff off the configured interval, capped (§6).
        return min(BACKOFF_CAP_SECONDS, float(self.poll_interval) * (2 ** min(failures, 6)))

    def _handle_auth_failure(self) -> float:
        """401 — reload the key from .env; if unchanged, go loud + degraded."""
        prior_key = getattr(self._company_cfg, "api_key", "")
        self._load_client()  # operator may have re-registered (.env rewritten)
        new_key = getattr(self._company_cfg, "api_key", "")

        if new_key and new_key != prior_key:
            with self._lock:
                self._auth_failing = False
            self.log(
                "company-inbox auth failure — reloaded a new key from .env, retrying",
                kind="company_inbox_auth_reload",
            )
            return float(self.poll_interval)

        degraded_interval = min(BACKOFF_CAP_SECONDS, self.poll_interval * DEGRADED_MULTIPLIER)
        with self._lock:
            self._auth_failing = True
            self._degraded = True
            first_time = not self._escalated
            self._escalated = True

        if first_time:
            # LOUD: this is the exact failure §1 exists to kill (agent silently
            # does no work). Distinct kind so the watchdog/supervisor surfaces
            # it; health() flips auth_valid=False; best-effort reporter alert.
            self.log(
                "company-inbox AUTH FAILING — api_key rejected (401). This agent "
                "will receive NO task assignments until the key is fixed "
                "(`jc company register`). Degraded poll every "
                f"{degraded_interval}s.",
                kind="company_inbox_auth_failure",
                level="warning",
            )
            self._best_effort_alert(degraded_interval)

        return float(degraded_interval)

    def _best_effort_alert(self, degraded_interval: float) -> None:
        """Try to raise a company alert. Likely 401s too (same bad key) — the
        WARN log + health() are the primary signal; this is a bonus."""
        client = self._client
        if client is None:
            return
        try:
            client.post_alert(
                {
                    "severity": "warning",
                    "title": "company-inbox auth failing",
                    "body": (
                        "api_key rejected (401); no task assignments will be "
                        f"received until re-registered. Degraded poll {int(degraded_interval)}s."
                    ),
                }
            )
        except Exception:  # noqa: BLE001
            pass

    # ── Helpers ──────────────────────────────────────────────────────────

    def _cfg_agent_id(self) -> str:
        """Raw read of COMPANY_AGENT_ID from the loaded config, or ''."""
        return str(getattr(self._company_cfg, "agent_id", "") or "") if self._company_cfg else ""

    def _agent_id(self) -> str:
        """Return this channel's persisted COMPANY_AGENT_ID.

        After spec `docs/specs/agent-self-discovery.md` §4.3, the fallback
        to ``instance_id`` is GONE. Discovery at boot is the only path that
        populates this value — by the time the poll loop calls
        ``_agent_id()`` the run() entrypoint has either confirmed the .env
        carries one, or refused to enter the loop.

        Callers outside the poll loop (start log line, etc.) may observe
        the empty string when discovery has not yet completed — that is
        intentional and surfaced via health().
        """
        return self._cfg_agent_id()

    def _load_client(self) -> None:
        self._company_cfg = company_conf.load(self.instance_dir)
        self._close_client()
        self._client = CompanyClient(self._company_cfg)

    # ── Boot-time agent_id discovery ────────────────────────────────────
    #
    # Spec: docs/specs/agent-self-discovery.md §4.
    #
    # An agent registered before PR #64 has no COMPANY_AGENT_ID in its
    # .env. Without that value the channel used to fall back to
    # instance_id (SHA256 of the dir), which the-company has never seen
    # and never will — every /inbox call 400s and no task is ever
    # delivered. Discovery removes that silent failure by asking the
    # backend "who am I, by UUID?" with our existing bearer.

    def _discover_agent_id(self, should_stop: Callable[[], bool]) -> bool:
        """Block until /api/agents/me returns our UUID, or until shutdown.

        Returns True on success (``.env`` persisted + cfg reloaded),
        False if ``should_stop()`` fires before we ever succeed.

        While discovery is failing we do NOT poll the inbox — the whole
        point of this routine is to avoid the silent 400 loop. Health
        surface flips to ``agent_id_discovered=false`` + degraded.
        """
        # Persistent error → degraded interval; transient single attempt
        # uses the normal poll interval as a floor (a fresh boot is the
        # cheap case and should retry quickly).
        degraded_interval = min(BACKOFF_CAP_SECONDS, self.poll_interval * DEGRADED_MULTIPLIER)
        attempts = 0
        while not should_stop():
            attempts += 1
            try:
                payload = self._client.whoami()
            except CompanyError as exc:
                # §4.1.b: 401 means the bearer is revoked. Try a one-shot
                # .env reload (operator may have just re-registered); if
                # the key didn't change there's no point hammering — fall
                # through to the degraded loud path on the next attempt.
                if getattr(exc, "status", 0) == 401:
                    prior_key = getattr(self._company_cfg, "api_key", "")
                    self._load_client()
                    new_key = getattr(self._company_cfg, "api_key", "")
                    if new_key and new_key != prior_key:
                        self.log(
                            "company-inbox discovery 401 — reloaded a new key "
                            "from .env, retrying immediately",
                            kind="company_inbox_discovery_auth_reload",
                        )
                        continue  # retry at once with the new key
                self._on_discovery_failure(exc, degraded_interval)
                self._sleep(float(degraded_interval), should_stop)
                continue
            except Exception as exc:  # noqa: BLE001 — never let discovery crash the thread
                self._on_discovery_failure(exc, degraded_interval)
                self._sleep(float(degraded_interval), should_stop)
                continue

            discovered_id = self._extract_agent_id(payload)
            if not discovered_id:
                self._on_discovery_failure(
                    ValueError(f"/me response missing id: {payload!r}"),
                    degraded_interval,
                )
                self._sleep(float(degraded_interval), should_stop)
                continue

            # Persist + reload. Failure to write is non-fatal: the value
            # is correct in memory for this run, next boot will retry.
            try:
                company_conf.write_env_keys(
                    self.instance_dir,
                    set_keys={"COMPANY_AGENT_ID": discovered_id},
                )
            except Exception as exc:  # noqa: BLE001 — spec §6.3
                self.log(
                    f"company-inbox discovered agent_id={discovered_id} but "
                    f".env write failed ({exc}); using in-memory for this run",
                    kind="company_inbox_discovery_persist_failed",
                    level="warning",
                )
                # Pin the cfg in memory so _agent_id() returns it this run.
                self._company_cfg = _replace_agent_id(self._company_cfg, discovered_id)
            else:
                self._load_client()  # reloads cfg from the freshly-written .env

            with self._lock:
                self._agent_id_discovered = True
                self._discovery_escalated = False
                self._degraded = False
                self._consecutive_failures = 0
                self._last_error = None

            self.log(
                f"company-inbox discovered agent_id={discovered_id} "
                f"after {attempts} attempt(s); polling resumes",
                kind="company_inbox_discovery_ok",
            )
            return True

        # Shutdown reached before discovery succeeded.
        return False

    def _on_discovery_failure(self, exc: Exception, degraded_interval: float) -> None:
        """Mark the channel degraded and escalate ONCE on persistent failure."""
        status = getattr(exc, "status", 0)
        with self._lock:
            self._consecutive_failures += 1
            self._last_error = f"discovery: status={status}: {exc}"
            self._degraded = True
            self._agent_id_discovered = False
            first_time = not self._discovery_escalated
            failures = self._consecutive_failures

        # Loud once at the third consecutive failure — matches §4.2 (and
        # the auth-failure escalation cadence). Any subsequent failures
        # stay silent until either success or operator action.
        if first_time and failures >= WARN_AFTER_CONSECUTIVE_FAILURES:
            with self._lock:
                self._discovery_escalated = True
            self.log(
                "company-inbox AGENT_ID DISCOVERY FAILING — /api/agents/me "
                f"returned {exc}. This agent will receive NO task assignments "
                f"until discovery succeeds. Degraded retry every "
                f"{degraded_interval}s.",
                kind="company_inbox_discovery_failure",
                level="warning",
            )

    def _close_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def _sleep(self, seconds: float, should_stop: Callable[[], bool]) -> None:
        """Sleep up to ``seconds``, waking ~1s to stay responsive to shutdown."""
        import time

        end = time.monotonic() + seconds
        while not should_stop():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def _extract_agent_id(self, payload: Any) -> str:
        """Pull ``id`` out of the /me response, tolerant of envelope shape.

        Accepts the canonical ``AgentOut`` dict, or a ``{"data": {...}}``
        envelope (server side may wrap a non-dict response).
        """
        if isinstance(payload, dict):
            value = payload.get("id")
            if value:
                return str(value)
            inner = payload.get("data")
            if isinstance(inner, dict) and inner.get("id"):
                return str(inner["id"])
        return ""

    # ── Health (watchdog integration) ────────────────────────────────────

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "auth_valid": not self._auth_failing,
                "degraded": self._degraded,
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
                "last_injected_at": self._last_injected_at,
                "seen_cache_size": len(self._seen),
                # Discovery surface (spec §4.2). False until the boot-time
                # /me call has populated COMPANY_AGENT_ID — operators
                # consume this to tell "channel is silent because of a
                # backend outage" from "channel is silent because it
                # never figured out its own UUID".
                "agent_id_discovered": self._agent_id_discovered,
            }


def _replace_agent_id(cfg: Any, agent_id: str) -> Any:
    """Return ``cfg`` with ``agent_id`` overridden.

    ``CompanyConfig`` is a frozen dataclass so we can't mutate in place.
    Tests inject ``SimpleNamespace`` for ``_company_cfg``; we accept both
    by trying dataclasses.replace first and falling back to a shallow
    rebind on the attribute.
    """
    try:
        return dataclasses.replace(cfg, agent_id=agent_id)
    except TypeError:
        # Not a dataclass (e.g. test SimpleNamespace) — set the attr.
        try:
            cfg.agent_id = agent_id  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return cfg


def _extract_items(result: Any) -> list[dict[str, Any]]:
    """Pull the task list out of the inbox response, tolerant of shape.

    Accepts ``{items: [...]}`` (spec), ``{tasks: [...]}``, or a bare list.
    """
    if isinstance(result, list):
        candidates = result
    elif isinstance(result, dict):
        candidates = result.get("items")
        if not isinstance(candidates, list):
            candidates = result.get("tasks")
        if not isinstance(candidates, list):
            candidates = result.get("data")
    else:
        candidates = None
    if not isinstance(candidates, list):
        return []
    return [t for t in candidates if isinstance(t, dict)]
