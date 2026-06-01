"""Tests for ``lib.gateway.liveness`` — see ``docs/specs/doctor-pid-liveness.md``."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.liveness import (  # noqa: E402
    Finding,
    all_liveness_findings,
    gateway_pid_finding,
    supervisor_pid_finding,
    telegram_409_finding,
)


def _write_pidfile(instance: Path, sub: str, name: str, pid: int) -> Path:
    d = instance / "state" / sub
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(f"{pid}\n", encoding="utf-8")
    return p


class GatewayPidFindingTests(unittest.TestCase):
    def test_missing_pidfile_is_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = gateway_pid_finding(Path(tmp))
            self.assertEqual(f.level, "info")
            self.assertIn("absent", f.message)

    def test_dead_pid_is_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            # PID 0 is never alive; use a sentinel guaranteed-dead PID.
            # Pick something huge — chance of collision is effectively zero.
            dead_pid = 2_147_400_000
            _write_pidfile(inst, "gateway", "jc-gateway.pid", dead_pid)
            f = gateway_pid_finding(inst)
            self.assertEqual(f.level, "fail")
            self.assertIn("dead", f.message)

    def test_corrupt_pidfile_is_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            d = inst / "state" / "gateway"
            d.mkdir(parents=True)
            (d / "jc-gateway.pid").write_text("not-a-number\n")
            f = gateway_pid_finding(inst)
            self.assertEqual(f.level, "fail")
            self.assertIn("corrupt", f.message)

    def test_live_pid_but_foreign_cmdline_is_fail(self):
        # Use our own PID — definitely alive, but cmdline contains "python",
        # not "jc-gateway". On platforms without /proc this collapses to OK,
        # so skip there to keep the assertion meaningful.
        if not Path("/proc").exists():
            self.skipTest("/proc not available")
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            _write_pidfile(inst, "gateway", "jc-gateway.pid", os.getpid())
            f = gateway_pid_finding(inst)
            self.assertEqual(f.level, "fail", msg=f"got {f}")
            self.assertIn("different process", f.message)

    def test_live_pid_matching_cmdline_is_ok(self):
        # Hand a pidfile pointing at /proc/1 (init) when its cmdline mentions
        # something we control — fragile across systems. Instead spawn a sleep
        # under a name we can match. We can't rename Python's argv easily, so
        # exec /bin/sleep with a wrapper script approach is overkill.
        # Use a custom marker — call _pid_finding directly with a marker that
        # matches our own cmdline.
        from gateway.liveness import _pid_finding

        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            d = inst / "state" / "gateway"
            d.mkdir(parents=True)
            pidfile = d / "jc-gateway.pid"
            pidfile.write_text(f"{os.getpid()}\n")
            # Read our own cmdline; pick a token that's certain to be there.
            if Path("/proc").exists():
                cmd = Path(f"/proc/{os.getpid()}/cmdline").read_bytes()
                token = cmd.split(b"\0", 1)[0].decode("utf-8", "replace")
                token = Path(token).name or "python"
            else:
                token = "python"
            f = _pid_finding(pidfile, label="x", cmdline_marker=token)
            self.assertEqual(f.level, "ok", msg=f"got {f}")


class SupervisorPidFindingTests(unittest.TestCase):
    def test_missing_pidfile_is_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(supervisor_pid_finding(Path(tmp)).level, "info")

    def test_dead_pid_is_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            _write_pidfile(inst, "supervisor", "jc-supervisor.pid", 2_147_400_001)
            f = supervisor_pid_finding(inst)
            self.assertEqual(f.level, "fail")


class Telegram409Tests(unittest.TestCase):
    def test_no_log_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(telegram_409_finding(Path(tmp)))

    def test_clean_log_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            d = inst / "state" / "gateway"
            d.mkdir(parents=True)
            (d / "gateway.log").write_text(
                "[2026-05-29T14:00:00Z] gateway daemon started pid=1234\n"
            )
            self.assertIsNone(telegram_409_finding(inst))

    def test_409_line_returns_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            d = inst / "state" / "gateway"
            d.mkdir(parents=True)
            (d / "gateway.log").write_text(
                "[2026-05-29T13:59:00Z] gateway daemon started pid=1234\n"
                "[2026-05-29T14:00:00Z] telegram poll error: HTTP Error 409: Conflict\n"
            )
            f = telegram_409_finding(inst)
            self.assertIsNotNone(f)
            self.assertEqual(f.level, "warn")
            self.assertIn("cross-instance", f.message)
            self.assertIn("409", f.message)

    def test_old_409_outside_tail_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            d = inst / "state" / "gateway"
            d.mkdir(parents=True)
            lines = ["[2026-05-29] HTTP Error 409: Conflict\n"]
            lines.extend(f"[2026-05-29] line {i}\n" for i in range(500))
            (d / "gateway.log").write_text("".join(lines))
            # Default tail_lines=200 should miss the leading 409.
            self.assertIsNone(telegram_409_finding(inst))


class AllLivenessFindingsTests(unittest.TestCase):
    def test_aggregates_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings = all_liveness_findings(Path(tmp))
            # Two pidfile findings (gateway + supervisor) — no 409 finding.
            self.assertEqual(len(findings), 2)
            self.assertTrue(all(isinstance(f, Finding) for f in findings))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
