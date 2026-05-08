"""Regression coverage for instance-local .env boundaries."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.config import clear_env_cache, env_value, merge_instance_env  # noqa: E402


class ConfigEnvBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_env_cache()

    def tearDown(self) -> None:
        clear_env_cache()

    def test_env_value_prefers_instance_env_over_process_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=instance-token\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "process-token"},
                clear=False,
            ):
                self.assertEqual(
                    env_value(instance, "TELEGRAM_BOT_TOKEN"),
                    "instance-token",
                )

    def test_env_value_falls_back_to_process_env_when_key_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text("OTHER=value\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "process-token"},
                clear=False,
            ):
                self.assertEqual(
                    env_value(instance, "TELEGRAM_BOT_TOKEN"),
                    "process-token",
                )

    def test_two_instances_under_same_user_resolve_their_own_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alpha = root / "alpha"
            beta = root / "beta"
            alpha.mkdir()
            beta.mkdir()
            (alpha / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=alpha-token\n",
                encoding="utf-8",
            )
            (beta / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=beta-token\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "ambient-token"},
                clear=False,
            ):
                self.assertEqual(env_value(alpha, "TELEGRAM_BOT_TOKEN"), "alpha-token")
                self.assertEqual(env_value(beta, "TELEGRAM_BOT_TOKEN"), "beta-token")

    def test_env_value_ignores_reserved_instance_runtime_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text(
                "PATH=/evil\n"
                "JC_EVENT_SOURCE=wrong\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"PATH": "/usr/bin:/bin", "JC_EVENT_SOURCE": "cron"},
                clear=False,
            ):
                self.assertEqual(env_value(instance, "PATH"), "/usr/bin:/bin")
                self.assertEqual(env_value(instance, "JC_EVENT_SOURCE"), "cron")

    def test_merge_instance_env_blocks_runtime_control_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=instance-token\n"
                "PATH=/evil\n"
                "RUNTIME_MODE=legacy-claude\n"
                "JC_EVENT_SOURCE=wrong\n",
                encoding="utf-8",
            )
            merged = merge_instance_env(
                instance,
                {
                    "TELEGRAM_BOT_TOKEN": "process-token",
                    "PATH": "/usr/bin:/bin",
                    "JC_EVENT_SOURCE": "cron",
                },
            )

            self.assertEqual(merged["TELEGRAM_BOT_TOKEN"], "instance-token")
            self.assertEqual(merged["PATH"], "/usr/bin:/bin")
            self.assertEqual(merged["JC_EVENT_SOURCE"], "cron")
            self.assertNotIn("RUNTIME_MODE", merged)


if __name__ == "__main__":
    unittest.main()
