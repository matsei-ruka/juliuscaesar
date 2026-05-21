"""Supervisor-driven the-company worker reporter.

Deterministic ``worker.started`` / ``worker.finished`` reports keyed off the
supervisor's existing event lifecycle (snapshot → finalize). Replaces the
brain-driven, agent-must-remember-to-call-the-CLI pattern.

Failure model: best-effort. Any HTTP / network / config error is logged via
``log_fn`` and the method returns ``False``. **The reporter never raises.**
A failing the-company backend cannot break the gateway tick.

Transport: stdlib ``urllib`` + ``json`` only — no new pip deps. 5s socket
timeout per call so a wedged backend can't stall the supervisor.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable, Optional


LogFn = Callable[[str], None]

# Hard cap on a single HTTP attempt. The supervisor tick has its own cadence,
# so a slow the-company backend must not stall it.
HTTP_TIMEOUT_SECONDS = 5.0


def _iso_z(ts: datetime) -> str:
    """Format a datetime as ISO-8601 ``...Z`` (UTC).

    Accepts naive or aware. Naive is assumed already UTC — matches every
    other supervisor timestamp which is built from ``datetime.now(UTC)``.
    """
    if ts.tzinfo is not None:
        # Normalize to UTC, then drop tzinfo for the Z suffix.
        from datetime import timezone

        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class Reporter:
    """Synchronous worker reporter for the supervisor's lifecycle.

    One instance per gateway process. The ``instance_boot_id`` is captured
    once at construction so every call within a process lifetime carries the
    same value — the backend upserts on ``(agent_id, instance_boot_id,
    remote_id)``, so re-using the same boot id is what lets a later
    ``worker.finished`` close a previously-opened ``worker.started``.
    """

    def __init__(
        self,
        api_url: str,
        agent_id: str,
        api_key: str,
        instance_boot_id: str,
        log_fn: LogFn | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.agent_id = agent_id
        self.api_key = api_key
        self.instance_boot_id = instance_boot_id
        self._log = log_fn or (lambda _msg: None)

    # --- public API --------------------------------------------------------

    def report_started(
        self,
        event_id: int,
        topic: str,
        started_at: datetime,
        brain: str | None,
        model: str | None,
    ) -> bool:
        """POST a ``status=running`` worker. Returns True on 2xx, else False."""
        worker: dict[str, Any] = {
            "remote_id": int(event_id),
            "topic": topic or "",
            "brain": brain or "",
            "model": model,
            "status": "running",
            "started_at": _iso_z(started_at),
        }
        return self._sync([worker])

    def report_finished(
        self,
        event_id: int,
        started_at: datetime,
        finished_at: datetime,
        brain: str | None,
        model: str | None,
    ) -> bool:
        """POST a ``status=finished`` worker. Returns True on 2xx, else False."""
        duration_ms = self._duration_ms(started_at, finished_at)
        worker: dict[str, Any] = {
            "remote_id": int(event_id),
            "brain": brain or "",
            "model": model,
            "status": "finished",
            "started_at": _iso_z(started_at),
            "finished_at": _iso_z(finished_at),
        }
        if duration_ms is not None:
            worker["duration_ms"] = duration_ms
        return self._sync([worker])

    # --- internals ---------------------------------------------------------

    def _sync(self, workers: list[dict[str, Any]]) -> bool:
        url = f"{self.api_url}/api/workers/sync"
        payload = {
            "instance_boot_id": self.instance_boot_id,
            "workers": workers,
        }
        try:
            body = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            self._log(f"company reporter encode error: {exc}")
            return False

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                status = getattr(resp, "status", 200)
                # Drain the response body so the socket can be reused/closed
                # cleanly. We don't actually care about the contents — server
                # returns the upserted rows, but our state lives locally.
                try:
                    resp.read()
                except Exception:  # noqa: BLE001
                    pass
                if 200 <= int(status) < 300:
                    return True
                self._log(f"company reporter http {status} for {url}")
                return False
        except urllib.error.HTTPError as exc:
            self._log(f"company reporter http error {exc.code}: {exc.reason}")
            return False
        except urllib.error.URLError as exc:
            self._log(f"company reporter url error: {exc.reason}")
            return False
        except Exception as exc:  # noqa: BLE001
            # Catch-all: socket.timeout, ConnectionResetError, anything weird.
            # The supervisor tick MUST NOT die because the-company is sad.
            self._log(f"company reporter unexpected error: {exc}")
            return False

    @staticmethod
    def _duration_ms(start: datetime, end: datetime) -> Optional[int]:
        try:
            delta = (end - start).total_seconds()
        except (TypeError, AttributeError):
            return None
        if delta < 0:
            return 0
        return int(delta * 1000)
