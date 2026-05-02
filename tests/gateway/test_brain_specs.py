"""Tests for canonical brain-spec parsing and config wiring.

Covers the contract from docs/specs/codex-main-brain-hardening.md §Phase 1:

- `default_brain: codex:gpt-5.4-mini` routes to brain=codex, model=gpt-5.4-mini.
- `channels.<name>.brain: codex:gpt-5.4-mini` preserves the model.
- `channels.<name>.brain: codex:gpt-5.4-mini` + `channels.<name>.model: gpt-5.5`
  fails config validation (no silent pick).
- `default_brain: bogus` fails config validation (no silent fallback to claude).
- `/brain gpt5` resolves to a current Codex model.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import brain_spec  # noqa: E402
from gateway import config as gateway_config  # noqa: E402
from gateway.brains.aliases import resolve_alias  # noqa: E402
from gateway.config import ConfigError, load_config  # noqa: E402


def _write_yaml(instance: Path, body: str) -> None:
    (instance / "ops").mkdir(exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(body)
    gateway_config.clear_config_cache()


class BrainSpecParserTests(unittest.TestCase):
    def test_bare_brain(self):
        spec = brain_spec.parse("codex")
        self.assertEqual(spec.brain, "codex")
        self.assertIsNone(spec.model)

    def test_brain_with_model(self):
        spec = brain_spec.parse("codex:gpt-5.4-mini")
        self.assertEqual(spec.brain, "codex")
        self.assertEqual(spec.model, "gpt-5.4-mini")

    def test_empty_inputs(self):
        self.assertEqual(brain_spec.parse(None), brain_spec.BrainSpec("", None))
        self.assertEqual(brain_spec.parse(""), brain_spec.BrainSpec("", None))
        self.assertEqual(brain_spec.parse("   "), brain_spec.BrainSpec("", None))

    def test_trailing_colon_no_model(self):
        spec = brain_spec.parse("codex:")
        self.assertEqual(spec.brain, "codex")
        self.assertIsNone(spec.model)

    def test_format_roundtrip(self):
        self.assertEqual(brain_spec.parse("codex").format(), "codex")
        self.assertEqual(brain_spec.parse("codex:gpt-5.4-mini").format(), "codex:gpt-5.4-mini")


class DefaultBrainConfigTests(unittest.TestCase):
    def test_default_brain_with_model_preserves_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "default_brain: codex:gpt-5.4-mini\n")
            cfg = load_config(instance)
            self.assertEqual(cfg.default_brain, "codex")
            self.assertEqual(cfg.default_model, "gpt-5.4-mini")

    def test_bare_default_brain_keeps_default_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "default_brain: codex\n"
                "default_model: gpt-5.4-mini\n",
            )
            cfg = load_config(instance)
            self.assertEqual(cfg.default_brain, "codex")
            self.assertEqual(cfg.default_model, "gpt-5.4-mini")

    def test_bogus_default_brain_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "default_brain: bogus\n")
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("default_brain", str(ctx.exception))
            self.assertIn("bogus", str(ctx.exception))

    def test_default_brain_with_model_plus_default_model_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "default_brain: codex:gpt-5.4-mini\n"
                "default_model: gpt-5.5\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("default_brain", str(ctx.exception))


class ChannelBrainConfigTests(unittest.TestCase):
    def test_channel_brain_with_model_preserves_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "default_brain: claude\n"
                "channels:\n"
                "  telegram:\n"
                "    enabled: true\n"
                "    token_env: TELEGRAM_BOT_TOKEN\n"
                "    brain: codex:gpt-5.4-mini\n",
            )
            cfg = load_config(instance)
            ch = cfg.channels["telegram"]
            self.assertEqual(ch.brain, "codex")
            self.assertEqual(ch.model, "gpt-5.4-mini")

    def test_channel_brain_with_model_plus_model_field_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "default_brain: claude\n"
                "channels:\n"
                "  telegram:\n"
                "    enabled: true\n"
                "    token_env: TELEGRAM_BOT_TOKEN\n"
                "    brain: codex:gpt-5.4-mini\n"
                "    model: gpt-5.5\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("channels.telegram.brain", str(ctx.exception))


class AliasTests(unittest.TestCase):
    def test_gpt5_alias_resolves_to_current_codex_model(self):
        # Spec §Phase 1 acceptance: `/brain gpt5` resolves to a current Codex
        # model. Catalog rotates, so we assert structure (codex:<modern model>)
        # rather than a frozen string — but the model must NOT be the stale
        # bare `gpt-5` placeholder the audit flagged.
        resolved = resolve_alias("gpt5")
        self.assertTrue(resolved.startswith("codex:"))
        self.assertNotEqual(resolved, "codex:gpt-5")

    def test_mini_alias_targets_codex_mini(self):
        self.assertEqual(resolve_alias("mini"), "codex:gpt-5.4-mini")
        self.assertEqual(resolve_alias("codex-mini"), "codex:gpt-5.4-mini")

    def test_codex_coding_alias(self):
        self.assertEqual(resolve_alias("codex-coding"), "codex:gpt-5.3-codex")

    def test_unknown_alias_passes_through(self):
        self.assertEqual(resolve_alias("custom:weird"), "custom:weird")


if __name__ == "__main__":
    unittest.main()
