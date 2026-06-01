"""Tests for the feature audit module."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from heartbeat import feature_audit  # noqa: E402


def _make_tasks(instance: Path, body: str) -> None:
    heartbeat = instance / "heartbeat"
    heartbeat.mkdir(parents=True, exist_ok=True)
    (heartbeat / "tasks.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


def _make_gateway(instance: Path, body: str) -> None:
    ops = instance / "ops"
    ops.mkdir(parents=True, exist_ok=True)
    (ops / "gateway.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


def _cron_block(basename: str, *task_names: str) -> str:
    lines = [f"# === JC-HEARTBEAT BEGIN instance={basename} ==="]
    for t in task_names:
        lines.append(f"30 3 * * * /usr/bin/jc heartbeat run {t} --instance-dir /x >> /x/log 2>&1")
    lines.append(f"# === JC-HEARTBEAT END instance={basename} ===")
    return "\n".join(lines) + "\n"


class FeatureAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.instance = self.root / "demo_inst"
        self.instance.mkdir()
        _make_gateway(
            self.instance,
            """
            timezone: UTC
            channels: {}
            """,
        )

    def _scan(self, crontab_text: str = ""):
        return feature_audit.scan(self.instance, crontab_reader=lambda: crontab_text)

    def test_disabled_builtin_detected(self) -> None:
        _make_tasks(
            self.instance,
            """
            tasks:
              dream_tick:
                builtin: dream_tick
                enabled: false
                schedule: "30 3 * * *"
              hot_tidy:
                builtin: hot_tidy
                enabled: false
            """,
        )
        features = self._scan(crontab_text="")
        by_name = {f.name: f for f in features}
        self.assertEqual(by_name["dream_tick"].status, "disabled")
        self.assertEqual(by_name["hot_tidy"].status, "disabled")

    def test_enabled_without_cron_is_disabled(self) -> None:
        """The Mikaela case — flag flipped on but no crontab entry."""
        _make_tasks(
            self.instance,
            """
            tasks:
              dream_tick:
                builtin: dream_tick
                enabled: true
                schedule: "30 3 * * *"
            """,
        )
        features = self._scan(crontab_text="# nothing relevant\n")
        by_name = {f.name: f for f in features}
        self.assertEqual(by_name["dream_tick"].status, "disabled")
        self.assertIn("no cron line", by_name["dream_tick"].where)

    def test_enabled_with_cron_is_enabled(self) -> None:
        _make_tasks(
            self.instance,
            """
            tasks:
              dream_tick:
                builtin: dream_tick
                enabled: true
                schedule: "30 3 * * *"
            """,
        )
        crontab = _cron_block(self.instance.name, "dream_tick")
        features = self._scan(crontab_text=crontab)
        by_name = {f.name: f for f in features}
        self.assertEqual(by_name["dream_tick"].status, "enabled")

    def test_missing_builtin_reported_as_missing(self) -> None:
        _make_tasks(self.instance, "tasks: {}\n")
        features = self._scan()
        by_name = {f.name: f for f in features}
        self.assertEqual(by_name["dream_tick"].status, "missing")

    def test_gateway_features_scanned(self) -> None:
        _make_tasks(self.instance, "tasks: {}\n")
        _make_gateway(
            self.instance,
            """
            actions:
              enabled: true
            entities:
              enabled: false
            channels:
              voice:
                enabled: true
              email:
                enabled: false
            """,
        )
        features = self._scan()
        by_name = {f.name: f for f in features}
        self.assertEqual(by_name["actions"].status, "enabled")
        self.assertEqual(by_name["entities"].status, "disabled")
        self.assertEqual(by_name["voice-channel"].status, "enabled")
        self.assertEqual(by_name["email-channel"].status, "disabled")
        # Unconfigured ones default disabled.
        self.assertEqual(by_name["accountabilities"].status, "disabled")

    def test_snapshot_diff_new_features(self) -> None:
        _make_tasks(self.instance, "tasks: {}\n")
        features = self._scan()
        # First run with no snapshot — everything is "new".
        snapshot = feature_audit.load_snapshot(self.instance)
        new = feature_audit.diff_new(features, snapshot)
        self.assertEqual(len(new), len(features))

        feature_audit.write_snapshot(self.instance, features)
        snapshot2 = feature_audit.load_snapshot(self.instance)
        new2 = feature_audit.diff_new(features, snapshot2)
        self.assertEqual(new2, [])

    def test_build_message_only_new_empty(self) -> None:
        self.assertIsNone(feature_audit.build_telegram_message([], only_new=True))

    def test_build_message_disabled_all_enabled(self) -> None:
        f = feature_audit.Feature(
            name="actions", status="enabled", where="ops/gateway.yaml", hint="x"
        )
        self.assertIsNone(feature_audit.build_telegram_message([f], only_new=False))

    def test_build_message_renders_bullets(self) -> None:
        f = feature_audit.Feature(
            name="dream_tick", status="disabled", where="x", hint="Nightly reflection"
        )
        body = feature_audit.build_telegram_message([f], only_new=True)
        assert body is not None
        self.assertIn("*New JC features available*", body)
        self.assertIn("• `dream_tick` — Nightly reflection", body)
        self.assertIn("Reply with the feature name", body)


if __name__ == "__main__":
    unittest.main()
