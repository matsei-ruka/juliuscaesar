"""Tests for the single crash-proof dispatch loop (GatewayRuntime.run_forever).

Audit Phase 1: `cmd_run` used to carry its own duplicated loop without the
code-drift check, and the loop body was unguarded — a transient exception
(sqlite "database is locked") killed the daemon. run_forever is now the only
production loop: exception-guarded with backoff, code-drift wired, requeue
tick included, SIGHUP reload + pidfile hook injected by the caller.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from gateway import runtime as runtime_module  # noqa: E402
from gateway.config import render_default_config  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402


def _instance() -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-runforever-"))
    (root / ".jc").write_text("", encoding="utf-8")
    (root / "ops").mkdir()
    (root / "ops" / "gateway.yaml").write_text(
        render_default_config(default_brain="claude:sonnet-4-6"),
        encoding="utf-8",
    )
    return root


class RunForeverTests(unittest.TestCase):
    def _runtime(self, *, max_iterations: int) -> GatewayRuntime:
        counter = {"n": 0}

        def stop_requested() -> bool:
            counter["n"] += 1
            return counter["n"] > max_iterations

        instance = _instance()
        rt = GatewayRuntime(
            instance,
            log_path=queue.queue_dir(instance) / "test.log",
            stop_requested=stop_requested,
        )
        # Keep the loop hermetic: no channel threads, no drift scan against
        # the real lib dir (its files are being edited by this very branch).
        rt.start_channels = mock.Mock()  # type: ignore[method-assign]
        rt._check_code_drift = mock.Mock()  # type: ignore[method-assign]
        return rt

    def test_loop_survives_dispatch_exception(self) -> None:
        rt = self._runtime(max_iterations=3)
        calls = {"n": 0}

        def flaky_dispatch() -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
            return False

        rt.dispatch_once = flaky_dispatch  # type: ignore[method-assign]
        with mock.patch.object(runtime_module.time, "sleep") as sleep_mock:
            rt.run_forever(poll_interval_seconds=0)
        # First call raised, loop kept going and dispatched again.
        self.assertGreaterEqual(calls["n"], 2)
        # Backoff sleep was applied after the error (2.0s for streak=1).
        self.assertIn(mock.call(2.0), sleep_mock.call_args_list)

    def test_systemexit_propagates_and_closes(self) -> None:
        # Code-drift contract: SystemExit(42) must escape the guard so the
        # process exits and the watchdog respawns with fresh modules.
        rt = self._runtime(max_iterations=10)
        rt.dispatch_once = mock.Mock(side_effect=SystemExit(42))  # type: ignore[method-assign]
        close_mock = mock.Mock()
        rt.close = close_mock  # type: ignore[method-assign]
        with mock.patch.object(runtime_module.time, "sleep"):
            with self.assertRaises(SystemExit) as ctx:
                rt.run_forever(poll_interval_seconds=0)
        self.assertEqual(ctx.exception.code, 42)
        close_mock.assert_called_once()

    def test_code_drift_checked_every_iteration(self) -> None:
        rt = self._runtime(max_iterations=3)
        rt.dispatch_once = mock.Mock(return_value=False)  # type: ignore[method-assign]
        with mock.patch.object(runtime_module.time, "sleep"):
            rt.run_forever(poll_interval_seconds=0)
        self.assertEqual(rt._check_code_drift.call_count, 3)

    def test_reload_request_consumed_once(self) -> None:
        rt = self._runtime(max_iterations=3)
        rt.dispatch_once = mock.Mock(return_value=False)  # type: ignore[method-assign]
        rt.reload_config = mock.Mock()  # type: ignore[method-assign]
        pending = {"flag": True}

        def consume() -> bool:
            if pending["flag"]:
                pending["flag"] = False
                return True
            return False

        with mock.patch.object(runtime_module.time, "sleep"):
            rt.run_forever(poll_interval_seconds=0, reload_requested=consume)
        rt.reload_config.assert_called_once()

    def test_reload_failure_does_not_kill_loop(self) -> None:
        rt = self._runtime(max_iterations=3)
        rt.dispatch_once = mock.Mock(return_value=False)  # type: ignore[method-assign]
        rt.reload_config = mock.Mock(side_effect=ValueError("bad yaml"))  # type: ignore[method-assign]
        with mock.patch.object(runtime_module.time, "sleep"):
            rt.run_forever(
                poll_interval_seconds=0, reload_requested=lambda: True
            )
        self.assertEqual(rt.dispatch_once.call_count, 3)

    def test_on_tick_hook_called(self) -> None:
        rt = self._runtime(max_iterations=2)
        rt.dispatch_once = mock.Mock(return_value=False)  # type: ignore[method-assign]
        tick = mock.Mock()
        with mock.patch.object(runtime_module.time, "sleep"):
            rt.run_forever(poll_interval_seconds=0, on_tick=tick)
        self.assertEqual(tick.call_count, 2)

    def test_requeue_tick_moves_expired_event_back_to_queued(self) -> None:
        rt = self._runtime(max_iterations=1)
        rt.dispatch_once = mock.Mock(return_value=False)  # type: ignore[method-assign]
        conn = queue.connect(rt.instance_dir)
        try:
            cur = conn.execute(
                """
                INSERT INTO events
                  (source, content, status, received_at, available_at, started_at,
                   locked_by, locked_until, retry_count)
                VALUES ('telegram', 'x', 'running', '2026-05-17T10:00:00Z',
                        '2026-05-17T10:00:00Z', '2026-05-17T10:00:00Z',
                        'dead-worker', '2026-05-17T10:05:00Z', 0)
                """,
            )
            conn.commit()
            eid = cur.lastrowid
        finally:
            conn.close()
        with mock.patch.object(runtime_module.time, "sleep"):
            rt.run_forever(poll_interval_seconds=0)
        conn = queue.connect(rt.instance_dir)
        try:
            row = conn.execute(
                "SELECT status, retry_count FROM events WHERE id=?", (eid,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["retry_count"], 1)


if __name__ == "__main__":
    unittest.main()
