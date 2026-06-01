"""Tests for the heartbeat cron sync module."""

from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from heartbeat import cron_sync  # noqa: E402


def _write_tasks(instance: Path, body: str) -> Path:
    heartbeat = instance / "heartbeat"
    heartbeat.mkdir(parents=True, exist_ok=True)
    p = heartbeat / "tasks.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


class CronSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.instance = self.root / "test_instance"
        self.instance.mkdir()
        # ops/ left empty — _resolve_instance_timezone falls back to "UTC".
        (self.instance / "ops").mkdir()

    def _sync(self, *, prior: str = "", dry_run: bool = False, tasks_yaml: str | None = None):
        if tasks_yaml is not None:
            _write_tasks(self.instance, tasks_yaml)
        captured: dict[str, str] = {}

        def reader() -> str:
            return prior

        def writer(text: str) -> None:
            captured["installed"] = text

        summary = cron_sync.sync(
            self.instance,
            dry_run=dry_run,
            jc_binary="/usr/bin/jc",
            timezone="Asia/Dubai",
            crontab_reader=reader,
            crontab_writer=writer,
        )
        return summary, captured

    BASE_TASKS = """\
        tasks:
          dream_tick:
            builtin: dream_tick
            enabled: true
            schedule: "30 3 * * *"
          hot_tidy:
            builtin: hot_tidy
            enabled: true
            schedule: "15 4 * * *"
          journal_tidy:
            builtin: journal_tidy
            enabled: false
            schedule: "30 4 * * *"
          self_model_run:
            builtin: self_model_run
            enabled: true
        """

    def test_idempotent_resync(self) -> None:
        first, captured = self._sync(tasks_yaml=self.BASE_TASKS)
        self.assertTrue(first["installed"])
        installed_text = captured["installed"]

        second, captured2 = self._sync(prior=installed_text, tasks_yaml=self.BASE_TASKS)
        self.assertFalse(second["installed"])
        self.assertEqual(second["crontab"], installed_text)
        self.assertNotIn("installed", captured2)

    def test_replaces_prior_block(self) -> None:
        first, captured = self._sync(tasks_yaml=self.BASE_TASKS)
        prior = captured["installed"]

        new_yaml = """\
            tasks:
              dream_tick:
                builtin: dream_tick
                enabled: true
                schedule: "0 4 * * *"
            """
        second, captured2 = self._sync(prior=prior, tasks_yaml=new_yaml)
        self.assertTrue(second["installed"])
        new_text = captured2["installed"]
        # Only one block, only one dream_tick line, with the new schedule.
        self.assertEqual(new_text.count(cron_sync.BEGIN_MARKER), 1)
        self.assertEqual(new_text.count("heartbeat run dream_tick"), 1)
        self.assertIn("0 4 * * * /usr/bin/jc heartbeat run dream_tick", new_text)
        self.assertNotIn("30 3 * * *", new_text)
        self.assertNotIn("hot_tidy", new_text)

    def test_disabled_task_skipped(self) -> None:
        summary, _ = self._sync(tasks_yaml=self.BASE_TASKS)
        self.assertNotIn("journal_tidy", summary["block"])

    def test_missing_schedule_skipped(self) -> None:
        summary, _ = self._sync(tasks_yaml=self.BASE_TASKS)
        # self_model_run has enabled: true but no schedule — must be skipped.
        self.assertNotIn("self_model_run", summary["block"])

    def test_dry_run_does_not_install(self) -> None:
        summary, captured = self._sync(tasks_yaml=self.BASE_TASKS, dry_run=True)
        self.assertFalse(summary["installed"])
        self.assertNotIn("installed", captured)
        # The would-be crontab still contains the block.
        self.assertIn("heartbeat run dream_tick", summary["crontab"])

    def test_preserves_external_lines(self) -> None:
        prior = "# user line\n0 0 * * * /bin/true\n"
        summary, captured = self._sync(prior=prior, tasks_yaml=self.BASE_TASKS)
        new_text = captured["installed"]
        self.assertIn("# user line", new_text)
        self.assertIn("0 0 * * * /bin/true", new_text)
        self.assertIn(cron_sync.BEGIN_MARKER, new_text)

    def test_empty_yaml_strips_existing_block(self) -> None:
        # Operator removed all schedules — block should disappear from crontab.
        first, captured = self._sync(tasks_yaml=self.BASE_TASKS)
        prior = captured["installed"]
        empty_yaml = "tasks: {}\n"
        summary, captured2 = self._sync(prior=prior, tasks_yaml=empty_yaml)
        new_text = captured2["installed"]
        self.assertNotIn(cron_sync.BEGIN_MARKER, new_text)

    def test_invalid_schedule_rejected(self) -> None:
        bad = """\
            tasks:
              dream_tick:
                builtin: dream_tick
                enabled: true
                schedule: "every minute"
            """
        with self.assertRaises(ValueError):
            self._sync(tasks_yaml=bad)


if __name__ == "__main__":
    unittest.main()
