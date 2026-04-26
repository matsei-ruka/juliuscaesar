"""Backoff + restart-budget arithmetic."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from watchdog.child import ChildSpec, ChildState, HealthSpec, RestartSpec  # noqa: E402
from watchdog import policy  # noqa: E402


def _spec(**restart_overrides) -> ChildSpec:
    restart = RestartSpec(**{
        "backoff": (5, 10, 30, 60, 300),
        "max_in_window": 5,
        "window_seconds": 600,
        "start_grace_seconds": 15,
        **restart_overrides,
    })
    return ChildSpec(name="x", type="daemon", health=HealthSpec(), restart=restart)


class BackoffTests(unittest.TestCase):
    def test_first_attempt_uses_first_slot(self):
        spec = _spec()
        state = ChildState(name="x", consecutive_failures=1)
        self.assertEqual(policy.backoff_for(spec, state), 5)

    def test_climbs_table(self):
        spec = _spec()
        state = ChildState(name="x", consecutive_failures=4)
        self.assertEqual(policy.backoff_for(spec, state), 60)

    def test_caps_at_last_slot(self):
        spec = _spec()
        state = ChildState(name="x", consecutive_failures=99)
        self.assertEqual(policy.backoff_for(spec, state), 300)


class RestartBudgetTests(unittest.TestCase):
    def test_within_budget_allows_restart(self):
        spec = _spec(max_in_window=5, window_seconds=600)
        state = ChildState(name="x", attempts_in_window=[100.0, 200.0, 300.0])
        self.assertTrue(policy.may_restart(spec, state, now=400.0))

    def test_at_budget_denies(self):
        spec = _spec(max_in_window=5, window_seconds=600)
        state = ChildState(name="x", attempts_in_window=[1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertFalse(policy.may_restart(spec, state, now=10.0))

    def test_window_trim_releases_budget(self):
        spec = _spec(max_in_window=5, window_seconds=60)
        # Five attempts a long time ago — outside the 60s window, should be trimmed.
        state = ChildState(name="x", attempts_in_window=[10.0, 20.0, 30.0, 40.0, 50.0])
        self.assertTrue(policy.may_restart(spec, state, now=200.0))
        self.assertEqual(state.attempts_in_window, [])

    def test_record_attempt_bumps_counters(self):
        spec = _spec()
        state = ChildState(name="x")
        policy.record_attempt(state, now=1000.0)
        policy.record_attempt(state, now=1010.0)
        self.assertEqual(state.consecutive_failures, 2)
        self.assertEqual(state.attempts_in_window, [1000.0, 1010.0])
        self.assertEqual(state.last_attempt_at, 1010.0)

    def test_attempt_due_respects_backoff(self):
        spec = _spec()
        state = ChildState(name="x", consecutive_failures=1, last_attempt_at=1000.0)
        self.assertFalse(policy.attempt_due(spec, state, now=1004.0))  # 4s < 5s
        self.assertTrue(policy.attempt_due(spec, state, now=1006.0))   # 6s > 5s

    def test_record_healthy_resets_failures(self):
        state = ChildState(name="x", consecutive_failures=4, attempts_in_window=[1.0, 2.0])
        policy.record_healthy(state)
        self.assertEqual(state.consecutive_failures, 0)
        # attempts_in_window stays — it's a sliding-window budget, not per-success.
        self.assertEqual(state.attempts_in_window, [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
