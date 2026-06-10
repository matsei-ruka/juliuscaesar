"""Audit feature 7 — config schema guard slice.

No silent YAML fallback, nested triage validation, explicit zeros
preserved, triage_cache_ttl_seconds accepted top-level, supervisor
section contents validated, validated atomic supervisor toggle.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.config import (  # noqa: E402
    ConfigError,
    clear_env_cache,
    load_config,
    validate_config,
)
from gateway.config_writer import set_supervisor_enabled  # noqa: E402


def _write(instance: Path, body: str) -> None:
    (instance / "ops").mkdir(parents=True, exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(body, encoding="utf-8")


class YamlSyntaxErrorTests(unittest.TestCase):
    def test_invalid_yaml_raises_instead_of_silent_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write(instance, "default_brain: claude\nchannels:\n  telegram: [unclosed\n")
            with self.assertRaises(ConfigError):
                load_config(instance)


class NestedTriageValidationTests(unittest.TestCase):
    def test_nested_threshold_out_of_range_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write(
                instance,
                "default_brain: claude\n"
                "triage:\n"
                "  backend: none\n"
                "  triage_confidence_threshold: 7\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                validate_config(instance)
            self.assertIn("triage_confidence_threshold", str(ctx.exception))

    def test_nested_fallback_brain_unknown_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write(
                instance,
                "default_brain: claude\n"
                "triage:\n"
                "  backend: none\n"
                "  default_fallback_brain: nonexistent_brain\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                validate_config(instance)
            self.assertIn("default_fallback_brain", str(ctx.exception))

    def test_cache_ttl_accepted_top_level(self):
        # Loader reads it top-level; the validator used to reject it there
        # (writer/validator drift — the supervisor-key incident family).
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write(
                instance,
                "default_brain: claude\n"
                "triage_cache_ttl_seconds: 60\n",
            )
            cfg = validate_config(instance)
            self.assertEqual(cfg.triage.cache_ttl_seconds, 60)


class ZeroPreservationTests(unittest.TestCase):
    def test_max_retries_zero_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write(
                instance,
                "default_brain: claude\n"
                "gateway:\n"
                "  max_retries: 0\n",
            )
            cfg = load_config(instance)
            self.assertEqual(cfg.max_retries, 0)

    def test_log_backups_zero_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write(
                instance,
                "default_brain: claude\n"
                "reliability:\n"
                "  log_backups: 0\n",
            )
            cfg = load_config(instance)
            self.assertEqual(cfg.reliability.log_backups, 0)


class SupervisorSectionValidationTests(unittest.TestCase):
    def test_unknown_supervisor_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write(
                instance,
                "default_brain: claude\n"
                "supervisor:\n"
                "  enabledd: true\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                validate_config(instance)
            self.assertIn("supervisor.enabledd", str(ctx.exception))

    def test_bogus_narrator_brain_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write(
                instance,
                "default_brain: claude\n"
                "supervisor:\n"
                "  narrator_brain: not_a_brain\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                validate_config(instance)
            self.assertIn("narrator_brain", str(ctx.exception))

    def test_valid_supervisor_section_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write(
                instance,
                "default_brain: claude\n"
                "supervisor:\n"
                "  enabled: true\n"
                "  narrator_brain: openrouter:deepseek-v4-flash\n"
                "  recovery:\n"
                "    enabled: true\n"
                "    fallback_brain: claude\n",
            )
            validate_config(instance)  # should not raise


class SupervisorToggleWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_env_cache()

    def test_toggle_writes_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write(instance, "default_brain: claude\n")
            self.assertTrue(set_supervisor_enabled(instance, True))
            cfg_text = (instance / "ops" / "gateway.yaml").read_text()
            self.assertIn("supervisor", cfg_text)
            validate_config(instance)  # written config validates
            self.assertFalse(set_supervisor_enabled(instance, True))

    def test_toggle_refuses_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write(
                instance,
                "default_brain: claude\n"
                "bogus_top_level_key: 1\n",
            )
            before = (instance / "ops" / "gateway.yaml").read_text()
            with self.assertRaises(ConfigError):
                set_supervisor_enabled(instance, True)
            after = (instance / "ops" / "gateway.yaml").read_text()
            self.assertEqual(before, after)  # nothing written


if __name__ == "__main__":
    unittest.main()
