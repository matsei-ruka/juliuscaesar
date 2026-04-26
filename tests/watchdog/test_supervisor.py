"""Supervisor — health → restart → alert mode loop."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from watchdog.child import StateStore  # noqa: E402
from watchdog.registry import registry_path  # noqa: E402
from watchdog.supervisor import Supervisor  # noqa: E402


def _make_instance(tmp: Path, *, yaml: str) -> Path:
    inst = tmp
    (inst / "ops").mkdir(parents=True, exist_ok=True)
    (inst / ".jc").write_text("", encoding="utf-8")
    registry_path(inst).write_text(yaml, encoding="utf-8")
    return inst


_HEALTHY_YAML = """children:
  - name: alpha
    type: daemon
    enabled: true
    start: /bin/true
    health:
      pid_alive: false
      heartbeat_file: state/alpha.heartbeat
      heartbeat_max_age_seconds: 30
    restart:
      backoff: [1, 2, 5]
      max_in_window: 3
      window_seconds: 60
      start_grace_seconds: 0
"""


class SupervisorTests(unittest.TestCase):
    def test_healthy_child_does_not_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = _make_instance(Path(tmp), yaml=_HEALTHY_YAML)
            (inst / "state").mkdir(parents=True, exist_ok=True)
            hb = inst / "state" / "alpha.heartbeat"
            hb.write_text("ok")  # mtime = now
            alerts: list[str] = []
            sup = Supervisor(
                inst,
                alert_fn=lambda msg, spec, st: alerts.append(msg),
                clock=lambda: time.time(),
                log=lambda m: None,
            )
            sup._do_tick()
            store = StateStore(inst)
            st = store.get("alpha")
            self.assertEqual(st.consecutive_failures, 0)
            self.assertEqual(alerts, [])

    def test_dead_child_restarts_with_backoff(self):
        # Heartbeat file missing → child is "down". Use /bin/true as the
        # restart command (returns 0). Run several ticks with synthetic
        # clock to confirm the backoff schedule is honored.
        with tempfile.TemporaryDirectory() as tmp:
            inst = _make_instance(Path(tmp), yaml=_HEALTHY_YAML)
            (inst / "state").mkdir(parents=True, exist_ok=True)
            now = [1000.0]
            sup = Supervisor(
                inst,
                alert_fn=lambda *a, **k: None,
                clock=lambda: now[0],
                log=lambda m: None,
            )
            sup._do_tick()  # first attempt — restart fires
            store = StateStore(inst)
            self.assertEqual(store.get("alpha").consecutive_failures, 1)
            # Immediately back: no second attempt because backoff is 1s.
            now[0] = 1000.5
            sup = Supervisor(
                inst,
                alert_fn=lambda *a, **k: None,
                clock=lambda: now[0],
                log=lambda m: None,
            )
            sup._do_tick()
            self.assertEqual(StateStore(inst).get("alpha").consecutive_failures, 1)
            # Wait past the 1s backoff, a second attempt fires.
            now[0] = 1002.0
            sup = Supervisor(
                inst,
                alert_fn=lambda *a, **k: None,
                clock=lambda: now[0],
                log=lambda m: None,
            )
            sup._do_tick()
            self.assertEqual(StateStore(inst).get("alpha").consecutive_failures, 2)

    def test_alert_mode_triggers_after_budget_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = _make_instance(Path(tmp), yaml=_HEALTHY_YAML)
            (inst / "state").mkdir(parents=True, exist_ok=True)
            now = [1000.0]
            alerts: list[str] = []

            def tick():
                nonlocal_clock = lambda: now[0]
                sup = Supervisor(
                    inst,
                    alert_fn=lambda msg, spec, st: alerts.append(msg),
                    clock=nonlocal_clock,
                    log=lambda m: None,
                )
                sup._do_tick()

            # Drive 4 attempts (within budget = 3 max).
            tick()
            now[0] += 5
            tick()
            now[0] += 10
            tick()
            now[0] += 30
            tick()  # 4th attempt — should hit budget and trip alert mode.
            self.assertEqual(len(alerts), 1)
            self.assertIn("alpha", alerts[0])
            store = StateStore(inst)
            self.assertTrue(store.get("alpha").alert_mode)
            # Subsequent ticks under alert mode no-op.
            now[0] += 60
            tick()
            self.assertEqual(len(alerts), 1)


if __name__ == "__main__":
    unittest.main()
