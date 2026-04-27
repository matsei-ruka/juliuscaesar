"""Background reporter thread: heartbeats, worker sync, conversation events.

Lifecycle (driven by ``GatewayRuntime``):

* ``start()`` — register if needed, spawn the ticker thread.
* ``on_conversation(event, response, meta)`` — called from ``dispatch_once``
  after a successful brain reply. Queues an in-memory ``conversation.message``
  pair (inbound + outbound) for the next batch.
* ``stop()`` — best-effort offline POST, signal stop, join.

Tick (every ``HEARTBEAT_INTERVAL_SECONDS``):

1. ``gateway.snapshot`` — queue depth, error rate, channels, etc.
2. Workers DB sync — emit ``worker.started`` / ``worker.finished`` for
   every row whose state has changed since last tick.
3. Drain queued ``conversation.message`` events.
4. Drain ``state/company/outbox/`` (replay buffered events on reconnect).

Failures are logged + buffered. The Company being down never breaks JC.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from gateway import queue as gateway_queue  # type: ignore
from workers import db as workers_db  # type: ignore

from . import conf as conf_module
from .client import CompanyClient, CompanyError
from .conf import (
    BATCH_MAX_EVENTS,
    HEARTBEAT_INTERVAL_SECONDS,
    CompanyConfig,
)

log = logging.getLogger("jc.company.reporter")


# ---------------------------------------------------------------------------
# UUIDv7 generator
# ---------------------------------------------------------------------------

def uuid7() -> str:
    """RFC 9562 UUIDv7 — 48-bit ms timestamp + 74 random bits.

    Stdlib doesn't have ``uuid.uuid7`` until Python 3.13; we target ``>=3.11``.
    """
    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    # version=7, variant=10 (RFC 4122)
    hi = (ts_ms << 16) | (0x7 << 12) | rand_a
    lo = (0b10 << 62) | rand_b
    raw = hi.to_bytes(8, "big") + lo.to_bytes(8, "big")
    return str(uuid.UUID(bytes=raw))


# ---------------------------------------------------------------------------
# Gateway snapshot — used by both the reporter heartbeat tick and CLI ping.
# ---------------------------------------------------------------------------


def build_snapshot(instance_dir: Path) -> dict[str, Any]:
    """Build a heartbeat-ready snapshot. Spec §4.1 fields, no ``status``."""
    try:
        conn = gateway_queue.connect(instance_dir)
        try:
            counts = gateway_queue.counts(conn)
        finally:
            conn.close()
        queue_depth = int(counts.get("queued", 0)) + int(counts.get("running", 0))
    except Exception:  # noqa: BLE001
        queue_depth = 0

    triage_backend, brain_runtime, channels_enabled = "", "", []
    try:
        from gateway.config import load_config  # type: ignore

        gw_cfg = load_config(instance_dir)
        triage_backend = gw_cfg.triage.backend or ""
        brain_runtime = gw_cfg.default_brain or ""
        channels_enabled = [name for name, ch in gw_cfg.channels.items() if ch.enabled]
    except Exception:  # noqa: BLE001
        pass

    return {
        "queue_depth": queue_depth,
        "brain_runtime": brain_runtime,
        "triage_backend": triage_backend,
        "channels_enabled": channels_enabled,
        "error_rate_5m": 0.0,
        "cpu_pct": 0.0,
        "memory_mb": 0,
    }


# ---------------------------------------------------------------------------
# Outbox: append-only JSON-lines, one file per UTC day.
# ---------------------------------------------------------------------------


class Outbox:
    """Disk-backed buffer for events that failed to POST.

    Layout: ``state/company/outbox/YYYY-MM-DD.jsonl``. Each line is a JSON
    object: ``{"event_type": ..., "payload": ..., "queued_at": ...}``.

    Eviction (``trim``) drops oldest files until size + age caps are met.
    """

    def __init__(self, instance_dir: Path, *, max_mb: int, max_age_hours: int):
        self.dir = Path(instance_dir) / "state" / "company" / "outbox"
        self.max_bytes = int(max_mb) * 1024 * 1024
        self.max_age_seconds = int(max_age_hours) * 3600
        self._lock = threading.Lock()

    def _today_path(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.dir / f"{day}.jsonl"

    def append(self, events: list[dict[str, Any]]) -> int:
        """Append ``events`` to today's file. Returns bytes written."""
        if not events:
            return 0
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            written = 0
            target = self._today_path()
            with target.open("a", encoding="utf-8") as fh:
                for evt in events:
                    line = json.dumps({**evt, "queued_at": now_iso}) + "\n"
                    fh.write(line)
                    written += len(line.encode("utf-8"))
            return written

    def files(self) -> list[Path]:
        if not self.dir.exists():
            return []
        return sorted(p for p in self.dir.glob("*.jsonl") if p.is_file())

    def total_bytes(self) -> int:
        total = 0
        for path in self.files():
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def trim(self) -> tuple[int, int]:
        """Drop-oldest until size + age caps are satisfied.

        Returns ``(events_dropped, bytes_reclaimed)``.
        """
        with self._lock:
            now = time.time()
            dropped_events = 0
            dropped_bytes = 0

            # Age-based eviction: any file whose mtime is older than cap.
            for path in self.files():
                try:
                    mtime = path.stat().st_mtime
                    size = path.stat().st_size
                except OSError:
                    continue
                if now - mtime > self.max_age_seconds:
                    dropped_events += _count_lines(path)
                    dropped_bytes += size
                    try:
                        path.unlink()
                    except OSError:
                        pass

            # Size-based eviction: drop oldest until under cap.
            files = self.files()
            total = sum(p.stat().st_size for p in files if p.exists())
            for path in files:
                if total <= self.max_bytes:
                    break
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                dropped_events += _count_lines(path)
                dropped_bytes += size
                try:
                    path.unlink()
                except OSError:
                    pass
                total -= size

            return dropped_events, dropped_bytes

    def drain(
        self,
        send: Callable[[list[dict[str, Any]]], None],
        *,
        batch: int = BATCH_MAX_EVENTS,
        since_mtime: float | None = None,
    ) -> int:
        """Replay buffered events through ``send``.

        ``send`` receives each batch as a list of ``{event_type, payload, ...}``
        dicts. Each chunk is sent independently — if chunk N succeeds and
        chunk N+1 raises, the file is rewritten with only the unsent tail so
        the successful chunks are not retransmitted on the next tick.

        ``since_mtime``: when set, files whose mtime is strictly older than
        the cutoff are skipped (left intact). Used by ``cmd_replay --since``.

        Returns the number of events successfully replayed.
        """
        replayed = 0
        with self._lock:
            for path in self.files():
                if since_mtime is not None:
                    try:
                        if path.stat().st_mtime < since_mtime:
                            continue
                    except OSError:
                        continue
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                events: list[dict[str, Any]] = []
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                if not events:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                # Send chunks independently; stop on first failure and persist
                # remaining chunks so we don't re-send the accepted ones.
                sent_count = 0
                failed = False
                for chunk_start in range(0, len(events), batch):
                    chunk = events[chunk_start : chunk_start + batch]
                    try:
                        send(chunk)
                    except Exception:  # noqa: BLE001
                        failed = True
                        break
                    sent_count += len(chunk)
                replayed += sent_count
                if failed:
                    self._rewrite_tail(path, events[sent_count:])
                    return replayed
                try:
                    path.unlink()
                except OSError:
                    pass
        return replayed

    def _rewrite_tail(self, path: Path, remaining: list[dict[str, Any]]) -> None:
        """Atomically replace ``path`` with only the unsent ``remaining`` events."""
        if not remaining:
            try:
                path.unlink()
            except OSError:
                pass
            return
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                for evt in remaining:
                    fh.write(json.dumps(evt) + "\n")
            tmp.replace(path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Workers DB cursor: track which rows we've already reported.
# ---------------------------------------------------------------------------


class WorkersCursor:
    """Tracks the (status, id) pairs we've already emitted to avoid dupes.

    Persisted at ``state/company/workers_cursor.json``. Stores the last-seen
    ``(id, status)`` per worker so a row that flips ``running -> done`` after
    we've already reported ``running`` produces exactly one ``finished``.

    Cached entries are scoped to ``instance_boot_id`` — after a gateway
    restart (which resets the workers SQLite autoincrement when the DB is
    wiped) the cache is discarded so fresh ``id=1`` rows are not silently
    suppressed by stale entries from a prior boot.
    """

    def __init__(self, instance_dir: Path, *, boot_id: str):
        self.path = Path(instance_dir) / "state" / "company" / "workers_cursor.json"
        self.boot_id = boot_id
        self._cache: dict[int, str] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("boot_id") == self.boot_id:
                cache = data.get("cache")
                if isinstance(cache, dict):
                    self._cache = {int(k): str(v) for k, v in cache.items()}
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            self._cache = {}
        self._loaded = True

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = {"boot_id": self.boot_id, "cache": self._cache}
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def diff(self, instance_dir: Path) -> list[tuple[str, dict[str, Any]]]:
        """Return ``(event_type, payload)`` tuples for each new/changed worker."""
        self._load()
        events: list[tuple[str, dict[str, Any]]] = []
        try:
            conn = workers_db.connect(instance_dir)
        except Exception:  # noqa: BLE001
            return events
        try:
            rows = conn.execute(
                "SELECT id, topic, brain, model, status, started_at, finished_at, "
                "exit_code, spawned_by, name, tags FROM workers "
                "ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()

        new_cache: dict[int, str] = {}
        for row in rows:
            wid = int(row["id"])
            status = row["status"]
            new_cache[wid] = status
            prior = self._cache.get(wid)
            if prior is None:
                # First sight of this worker. Emit started; if already terminal,
                # also emit finished in the same tick (e.g. fast adapters that
                # finished between our previous tick and this one).
                events.append(("worker.started", _started_payload(row)))
                if status in ("done", "failed", "cancelled", "need_input"):
                    events.append(("worker.finished", _finished_payload(row)))
            elif prior != status and status in ("done", "failed", "cancelled", "need_input"):
                events.append(("worker.finished", _finished_payload(row)))
        self._cache = new_cache
        self._save()
        return events


def _started_payload(row: Any) -> dict[str, Any]:
    tags = []
    if row["tags"]:
        try:
            parsed = json.loads(row["tags"])
            if isinstance(parsed, list):
                tags = [str(t) for t in parsed]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return {
        "remote_id": int(row["id"]),
        "topic": row["topic"],
        "name": row["name"] or None,
        "tags": tags,
        "brain": row["brain"],
        "model": row["model"] or None,
        "spawned_by": row["spawned_by"] or None,
        "started_at": row["started_at"],
    }


def _finished_payload(row: Any) -> dict[str, Any]:
    duration_ms: Optional[int] = None
    if row["started_at"] and row["finished_at"]:
        try:
            t0 = datetime.strptime(row["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            t1 = datetime.strptime(row["finished_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            duration_ms = int((t1 - t0).total_seconds() * 1000)
        except (TypeError, ValueError):
            duration_ms = None
    return {
        "remote_id": int(row["id"]),
        "status": row["status"],
        "exit_code": row["exit_code"],
        "duration_ms": duration_ms,
        "finished_at": row["finished_at"],
    }


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


class Reporter:
    """Background reporter for one instance.

    Owns: a ``CompanyClient``, an ``Outbox``, a ``WorkersCursor``, and a
    daemon thread that ticks every ``HEARTBEAT_INTERVAL_SECONDS``.

    Concurrency model: a single thread does all the I/O. Public methods
    called from the gateway hot path (``on_conversation``) just enqueue to
    a thread-safe in-memory list.
    """

    HEARTBEAT_INTERVAL_SECONDS = HEARTBEAT_INTERVAL_SECONDS

    def __init__(
        self,
        instance_dir: Path,
        *,
        cfg: Optional[CompanyConfig] = None,
        log_event: Optional[Callable[..., None]] = None,
    ):
        self.instance_dir = Path(instance_dir)
        self.cfg = cfg or conf_module.load(self.instance_dir)
        self.log = log_event or _stdlog
        self.instance_boot_id = uuid7()

        self.client = CompanyClient(self.cfg)
        self.outbox = Outbox(
            self.instance_dir,
            max_mb=self.cfg.outbox_max_mb,
            max_age_hours=self.cfg.outbox_max_age_hours,
        )
        self.workers_cursor = WorkersCursor(self.instance_dir, boot_id=self.instance_boot_id)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending_lock = threading.Lock()
        self._pending_events: list[dict[str, Any]] = []
        self._eviction_warn_at: float = 0.0
        self._last_register_attempt: float = 0.0

    # Backoff between registration attempts when the bootstrap call fails.
    REGISTER_RETRY_SECONDS: float = 60.0

    # --- Lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Register if no API key, then spawn the ticker thread.

        Registration failures don't crash — the reporter just stays
        unauthenticated and quietly buffers events to the outbox until the
        operator fixes credentials.
        """
        if self._thread is not None and self._thread.is_alive():
            return

        if not self.cfg.api_key and self.cfg.enrollment_token:
            self._last_register_attempt = time.monotonic()
            try:
                self._register()
            except CompanyError as exc:
                self.log(
                    f"company register failed: status={exc.status} {exc}",
                    kind="company_register_error",
                )

        self._stop.clear()
        thread = threading.Thread(target=self._loop, daemon=True, name="company-reporter")
        thread.start()
        self._thread = thread
        self.log("company reporter started", kind="company_start")

    def stop(self) -> None:
        """Signal the thread, join it, then post offline + close.

        Order matters: the reporter thread shares ``self.client`` with us,
        and ``requests.Session`` is not safe to use concurrently from two
        threads. Joining first guarantees the thread is no longer touching
        the session when we POST offline and close it.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        try:
            self.client.post_offline(self.snapshot())
        except Exception:  # noqa: BLE001
            pass
        self.client.close()

    # --- Hot-path hooks ----------------------------------------------------

    def on_conversation(self, event: Any, response: str, meta: dict[str, Any]) -> None:
        """Queue inbound + outbound conversation messages for the next tick.

        Safe to call from the gateway dispatch loop. Cheap: append-under-lock
        only. The expensive work (HTTP) happens on the reporter thread.
        """
        if not self._should_record_conversation(event, meta):
            return
        try:
            inbound = _build_conversation(event, meta, direction="inbound", cfg=self.cfg)
            outbound = _build_conversation_outbound(event, meta, response=response, cfg=self.cfg)
        except Exception as exc:  # noqa: BLE001
            self.log(f"company conversation build error: {exc}", kind="company_error")
            return
        with self._pending_lock:
            self._pending_events.append({"event_type": "conversation.message", "payload": inbound})
            self._pending_events.append({"event_type": "conversation.message", "payload": outbound})

    def raise_alert(self, *, severity: str, title: str, body: str = "", link: str = "") -> dict[str, Any]:
        """Synchronous alert POST. Used by ``alerts.py`` + the CLI."""
        return self.client.post_alert(
            {
                "severity": severity,
                "title": title,
                "body": body or None,
                "link": link or None,
            }
        )

    def raise_approval(
        self,
        *,
        type_: str,
        title: str,
        payload: dict[str, Any],
        body: str = "",
        expires_in_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        """Synchronous approval POST. Returns ``{approval_id, callback_token}``."""
        request_body = {
            "type": type_,
            "title": title,
            "body": body or None,
            "payload": payload,
        }
        if expires_in_seconds is not None:
            request_body["expires_in_seconds"] = expires_in_seconds
        return self.client.post_approval(request_body)

    # --- Snapshots ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Build a ``gateway.snapshot`` payload from the local instance."""
        return build_snapshot(self.instance_dir)

    # --- Internals ---------------------------------------------------------

    def _register(self) -> None:
        result = self.client.register(
            instance_id=conf_module.instance_id(self.instance_dir),
            name=conf_module.instance_name(self.instance_dir),
            framework=self.cfg.framework,
            framework_version=conf_module.framework_version(),
            enrollment_token=self.cfg.enrollment_token,
        )
        api_key = result.get("api_key")
        if not api_key:
            raise CompanyError("register: server returned no api_key", body=str(result))
        # Persist the key, drop the bootstrap token, refresh in-process cfg.
        conf_module.write_env_keys(
            self.instance_dir,
            set_keys={"COMPANY_API_KEY": str(api_key)},
            unset_keys=("COMPANY_ENROLLMENT_TOKEN",),
        )
        self.cfg = conf_module.load(self.instance_dir)
        self.client = CompanyClient(self.cfg)
        self.log(
            f"company registered agent_id={result.get('agent_id', '?')}",
            kind="company_registered",
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self.log(f"company tick error: {exc}", kind="company_error")
            if self._stop.wait(self.HEARTBEAT_INTERVAL_SECONDS):
                return

    def _tick(self) -> None:
        # Try (or retry) registration if we have a token but no key yet.
        if not self.cfg.api_key and self.cfg.enrollment_token:
            now = time.monotonic()
            if now - self._last_register_attempt >= self.REGISTER_RETRY_SECONDS:
                self._last_register_attempt = now
                try:
                    self._register()
                except CompanyError as exc:
                    self.log(
                        f"company register failed: status={exc.status} {exc}",
                        kind="company_register_error",
                    )

        if not self.cfg.api_key:
            # Unauthenticated: park any conversation events the gateway has
            # already produced into the outbox so they replay once we
            # register. Spec §6.2.
            with self._pending_lock:
                queued = self._pending_events
                self._pending_events = []
            if queued:
                self.outbox.append(queued)
            return

        events: list[dict[str, Any]] = []

        # 1. Snapshot.
        events.append({"event_type": "gateway.snapshot", "payload": self.snapshot()})

        # 2. Worker deltas.
        for evt_type, payload in self.workers_cursor.diff(self.instance_dir):
            if evt_type == "worker.started":
                payload = {"instance_boot_id": self.instance_boot_id, **payload}
            else:
                payload = {"instance_boot_id": self.instance_boot_id, **payload}
            events.append({"event_type": evt_type, "payload": payload})

        # 3. Drain conversation queue.
        with self._pending_lock:
            queued = self._pending_events
            self._pending_events = []
        events.extend(queued)

        # 4. Send.
        self._send_or_buffer(events)

        # 5. Replay outbox best-effort (separate POST so a current send
        #    failure doesn't double-buffer the just-buffered events).
        def _replay_send(batch: list[dict[str, Any]]) -> None:
            result = self.client.post_events(batch)
            self._handle_rejected(batch, result)

        try:
            replayed = self.outbox.drain(_replay_send)
            if replayed:
                self.log(f"company outbox replay sent={replayed}", kind="company_replay")
        except Exception as exc:  # noqa: BLE001
            self.log(f"company outbox drain error: {exc}", kind="company_error")

        # 6. Trim outbox by size + age.
        dropped, reclaimed = self.outbox.trim()
        if dropped:
            self._maybe_warn_eviction(dropped, reclaimed)

    def _send_or_buffer(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        for chunk_start in range(0, len(events), BATCH_MAX_EVENTS):
            chunk = events[chunk_start : chunk_start + BATCH_MAX_EVENTS]
            try:
                result = self.client.post_events(chunk)
            except CompanyError as exc:
                self.outbox.append(chunk)
                self.log(
                    f"company send failed status={exc.status}; buffered {len(chunk)} events",
                    kind="company_buffered",
                )
                continue
            self._handle_rejected(chunk, result)

    def _handle_rejected(
        self, chunk: list[dict[str, Any]], result: dict[str, Any]
    ) -> None:
        """Park server-rejected events in the DLQ — never re-buffer them."""
        rejected = result.get("rejected") if isinstance(result, dict) else None
        if not rejected:
            return
        first_reason = ""
        dead: list[dict[str, Any]] = []
        for entry in rejected:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(chunk):
                continue
            reason = str(entry.get("reason") or "")
            if not first_reason and reason:
                first_reason = reason
            dead.append({**chunk[idx], "rejected_reason": reason})
        if not dead:
            return
        self._write_dlq(dead)
        self.log(
            f"company events rejected count={len(dead)} first_reason={first_reason!r}",
            kind="company_rejected",
        )

    def _write_dlq(self, events: list[dict[str, Any]]) -> None:
        dlq_dir = self.instance_dir / "state" / "company" / "dlq"
        try:
            dlq_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            path = dlq_dir / f"{day}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                for evt in events:
                    fh.write(json.dumps(evt) + "\n")
        except OSError as exc:
            self.log(f"company dlq write error: {exc}", kind="company_error")

    def _should_record_conversation(self, event: Any, meta: dict[str, Any]) -> bool:
        # Note: do NOT gate on api_key. Pre-registration the conversation is
        # buffered to the outbox by _tick and replayed once we have a key.
        channel = _channel_name(event, meta)
        if channel in self.cfg.exclude_channels:
            return False
        user_id = str(getattr(event, "user_id", "") or meta.get("user_id") or "")
        if user_id and user_id in self.cfg.exclude_users:
            return False
        return True

    def _maybe_warn_eviction(self, dropped: int, reclaimed: int) -> None:
        now = time.time()
        if now - self._eviction_warn_at < 60:
            return
        self._eviction_warn_at = now
        self.log(
            f"company outbox evicted {dropped} events ({reclaimed} bytes)",
            kind="company_eviction",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _channel_name(event: Any, meta: dict[str, Any]) -> str:
    delivery = meta.get("delivery_channel") if isinstance(meta, dict) else None
    if delivery:
        return str(delivery)
    source = getattr(event, "source", None)
    return str(source or "unknown")


def _redact(text: str) -> str:
    """Replace conversation content with sha256 hash when redaction is on."""
    import hashlib

    if not text:
        return ""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"


def _build_conversation(event: Any, meta: dict[str, Any], *, direction: str, cfg: CompanyConfig) -> dict[str, Any]:
    channel = _channel_name(event, meta)
    user_id = str(getattr(event, "user_id", "") or meta.get("user_id") or "")
    user_label = meta.get("user_label") or meta.get("display_name") or None
    content = getattr(event, "content", "") or ""
    if cfg.redact_conversations:
        content = _redact(content)
    else:
        content = _truncate(content, cfg.conversation_max_chars)
    sent_at = (
        getattr(event, "received_at", None)
        or meta.get("received_at")
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return {
        "channel": channel,
        "user_id": user_id,
        "user_label": user_label,
        "direction": direction,
        "content": content,
        "content_redacted": cfg.redact_conversations,
        "media": meta.get("media") or None,
        "triage_class": meta.get("triage_class") or None,
        "brain": meta.get("brain") or None,
        "sent_at": sent_at,
    }


def _build_conversation_outbound(
    event: Any, meta: dict[str, Any], *, response: str, cfg: CompanyConfig
) -> dict[str, Any]:
    payload = _build_conversation(event, meta, direction="outbound", cfg=cfg)
    if cfg.redact_conversations:
        payload["content"] = _redact(response or "")
    else:
        payload["content"] = _truncate(response or "", cfg.conversation_max_chars)
    payload["sent_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return payload


def _stdlog(message: str, **fields: Any) -> None:
    log.info(message, extra={k: v for k, v in fields.items() if v is not None})
