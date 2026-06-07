"""§9 context profiles — registry resolution + session ceiling."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.lifecycle import profiles  # noqa: E402


class RegistryTest(unittest.TestCase):
    def test_default_catalog_has_expected_models(self) -> None:
        reg = profiles.ProfileRegistry()
        models = {p.model for p in reg.all()}
        self.assertEqual(
            models,
            {
                "claude-opus-4-8",
                "claude-opus-4-7-1m",
                "claude-sonnet-4-6",
                "claude-haiku-4-5",
                "gpt-5.4",
                "gpt-5.5",
                "gemini-2.5-pro",
            },
        )

    def test_for_model_returns_standard_variant(self) -> None:
        reg = profiles.ProfileRegistry()
        prof = reg.for_model("claude-sonnet-4-6")
        self.assertIsNotNone(prof)
        assert prof is not None
        self.assertEqual(prof.variant, "standard")
        self.assertEqual(prof.input_capacity_tokens, 200_000)

    def test_opus_extended_profile_enabled_by_default(self) -> None:
        reg = profiles.ProfileRegistry()
        enabled = {p.variant for p in reg.enabled_for_model("claude-opus-4-8")}
        self.assertEqual(enabled, {"standard", "extended"})

    def test_from_config_can_disable_extended(self) -> None:
        reg = profiles.ProfileRegistry.from_config(
            {"claude-opus-4-8-extended": {"enabled": False}}
        )
        enabled = {p.variant for p in reg.enabled_for_model("claude-opus-4-8")}
        self.assertEqual(enabled, {"standard"})

    def test_session_ceiling_is_largest_enabled(self) -> None:
        reg = profiles.ProfileRegistry()
        selected = reg.for_model("claude-opus-4-8")
        ceiling = profiles.session_ceiling(reg, model="claude-opus-4-8", selected=selected)
        self.assertIsNotNone(ceiling)
        assert ceiling is not None
        self.assertEqual(ceiling.input_capacity_tokens, 1_000_000)

    def test_session_ceiling_falls_back_to_selected(self) -> None:
        reg = profiles.ProfileRegistry()
        selected = reg.for_model("claude-haiku-4-5")
        ceiling = profiles.session_ceiling(reg, model="claude-haiku-4-5", selected=selected)
        self.assertEqual(ceiling, selected)


if __name__ == "__main__":
    unittest.main()
