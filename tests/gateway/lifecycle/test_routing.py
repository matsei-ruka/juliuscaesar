"""§10–§11 routing/lifecycle pressure decision table."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.lifecycle import profiles, routing  # noqa: E402


SMALL = profiles.ContextProfile(
    key="small", model="m-small", input_capacity_tokens=100_000
)
BIG = profiles.ContextProfile(
    key="big", model="m-big", input_capacity_tokens=1_000_000
)


class ThresholdValidationTest(unittest.TestCase):
    def test_strictly_increasing_ok(self) -> None:
        self.assertEqual(routing.Thresholds().validate(), [])

    def test_non_increasing_rejected(self) -> None:
        bad = routing.Thresholds(observe_ratio=0.7, rotate_ratio=0.6)
        self.assertTrue(bad.validate())

    def test_out_of_range_rejected(self) -> None:
        bad = routing.Thresholds(emergency_ratio=1.2)
        self.assertTrue(bad.validate())


class PressureTest(unittest.TestCase):
    def test_required_context_sums_reserves(self) -> None:
        req = routing.required_context(
            last_effective_input=50_000,
            estimated_new_prompt=1_000,
            reserves=routing.Reserves(output_tokens=16_000, turn_input_tokens=12_000),
        )
        self.assertEqual(req, 79_000)

    def test_fresh_session_always_dispatches(self) -> None:
        d = routing.evaluate_pressure(
            selected_profile=SMALL,
            ceiling=SMALL,
            required=10_000,
            current_context=0,
            thresholds=routing.Thresholds(),
            resumed=False,
        )
        self.assertEqual(d.action, routing.DISPATCH)

    def test_fitting_profile_dispatches(self) -> None:
        d = routing.evaluate_pressure(
            selected_profile=SMALL,
            ceiling=SMALL,
            required=40_000,
            current_context=40_000,
            thresholds=routing.Thresholds(),
            resumed=True,
        )
        self.assertEqual(d.action, routing.DISPATCH)

    def test_overflow_upgrades_to_larger_compatible(self) -> None:
        d = routing.evaluate_pressure(
            selected_profile=SMALL,
            ceiling=BIG,
            required=150_000,  # exceeds SMALL capacity
            current_context=130_000,
            thresholds=routing.Thresholds(),
            resumed=True,
            larger_profiles=[BIG],
        )
        self.assertEqual(d.action, routing.UPGRADE)
        self.assertEqual(d.upgrade_profile, BIG)

    def test_emergency_rotate_at_ceiling(self) -> None:
        d = routing.evaluate_pressure(
            selected_profile=SMALL,
            ceiling=SMALL,
            required=40_000,
            current_context=90_000,  # 0.9 of ceiling > emergency 0.85
            thresholds=routing.Thresholds(),
            resumed=True,
        )
        self.assertEqual(d.action, routing.EMERGENCY_ROTATE)

    def test_overflow_without_larger_fails(self) -> None:
        d = routing.evaluate_pressure(
            selected_profile=SMALL,
            ceiling=SMALL,
            required=150_000,  # exceeds SMALL, but lifecycle pressure stays low
            current_context=50_000,
            thresholds=routing.Thresholds(),
            resumed=True,
            larger_profiles=[],
        )
        self.assertEqual(d.action, routing.FAIL)


if __name__ == "__main__":
    unittest.main()
