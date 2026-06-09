"""Tests for ``lib.watchdog.install`` — marker-block crontab management.

See ``docs/specs/watchdog-self-install.md``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from watchdog.install import (  # noqa: E402
    BEGIN_MARKER,
    END_MARKER,
    build_block,
    compose_crontab,
    install,
    strip_block,
    verify,
)


class FakeCrontab:
    def __init__(self, initial: str = ""):
        self.text = initial
        self.writes: list[str] = []

    def read(self) -> str:
        return self.text

    def write(self, text: str) -> None:
        self.writes.append(text)
        self.text = text


class BuildBlockTests(unittest.TestCase):
    def test_block_has_markers_tick_reboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "rachel_zane"
            inst.mkdir()
            block = build_block(inst, jc_binary="/usr/local/bin/jc")
            self.assertIn(f"{BEGIN_MARKER}rachel_zane", block)
            self.assertIn(f"{END_MARKER}rachel_zane", block)
            self.assertIn("*/2 * * * *", block)
            self.assertIn("@reboot", block)
            self.assertIn("watchdog tick", block)
            self.assertIn(str(inst), block)
            self.assertTrue(block.endswith("\n"))

    def test_block_honors_custom_tick_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "alex"
            inst.mkdir()
            block = build_block(
                inst, jc_binary="/usr/local/bin/jc", tick_interval_minutes=5
            )
            self.assertIn("*/5 * * * *", block)
            self.assertNotIn("*/2 * * * *", block)

    def test_invalid_tick_interval_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "x"
            inst.mkdir()
            with self.assertRaises(ValueError):
                build_block(inst, jc_binary="/jc", tick_interval_minutes=0)
            with self.assertRaises(ValueError):
                build_block(inst, jc_binary="/jc", tick_interval_minutes=60)


class StripBlockTests(unittest.TestCase):
    def test_strip_removes_marker_block(self):
        text = (
            "# user line\n"
            f"{BEGIN_MARKER}rachel_zane ===\n"
            "*/2 * * * * /usr/local/bin/jc watchdog tick --instance-dir /x\n"
            f"{END_MARKER}rachel_zane ===\n"
            "# other line\n"
        )
        out = strip_block(text, "rachel_zane")
        self.assertNotIn(BEGIN_MARKER, out)
        self.assertIn("# user line\n", out)
        self.assertIn("# other line\n", out)

    def test_strip_removes_legacy_tag_lines(self):
        text = (
            "# user line\n"
            "@reboot /home/x/.local/bin/jc-watchdog tick "
            "--instance-dir /home/x/rachel_zane "
            "# jc-watchdog for /home/x/rachel_zane\n"
            "# unrelated\n"
        )
        out = strip_block(text, "rachel_zane")
        self.assertNotIn("jc-watchdog tick", out)
        self.assertIn("# user line\n", out)
        self.assertIn("# unrelated\n", out)

    def test_strip_preserves_other_instances(self):
        text = (
            f"{BEGIN_MARKER}rachel_zane ===\n"
            "*/2 * * * * /jc watchdog tick --instance-dir /a\n"
            f"{END_MARKER}rachel_zane ===\n"
            f"{BEGIN_MARKER}alex ===\n"
            "*/2 * * * * /jc watchdog tick --instance-dir /b\n"
            f"{END_MARKER}alex ===\n"
        )
        out = strip_block(text, "rachel_zane")
        self.assertNotIn(f"{BEGIN_MARKER}rachel_zane", out)
        self.assertIn(f"{BEGIN_MARKER}alex", out)


class ComposeCrontabTests(unittest.TestCase):
    def test_compose_appends_block_to_clean_crontab(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "rachel_zane"
            inst.mkdir()
            block = build_block(inst, jc_binary="/jc")
            new = compose_crontab("# user line\n", block, "rachel_zane")
            self.assertTrue(new.startswith("# user line\n"))
            self.assertIn(f"{BEGIN_MARKER}rachel_zane", new)

    def test_compose_replaces_existing_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "rachel_zane"
            inst.mkdir()
            old_block = (
                f"{BEGIN_MARKER}rachel_zane ===\n"
                "*/9 * * * * stale\n"
                f"{END_MARKER}rachel_zane ===\n"
            )
            block = build_block(inst, jc_binary="/jc")
            new = compose_crontab(old_block, block, "rachel_zane")
            self.assertNotIn("*/9 * * * * stale", new)
            self.assertIn("*/2 * * * *", new)


class InstallTests(unittest.TestCase):
    def test_install_writes_block_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "rachel_zane"
            inst.mkdir()
            cron = FakeCrontab("")
            result = install(
                inst,
                jc_binary="/usr/local/bin/jc",
                crontab_reader=cron.read,
                crontab_writer=cron.write,
            )
            self.assertTrue(result["installed"])
            self.assertEqual(len(cron.writes), 1)
            self.assertIn(BEGIN_MARKER, cron.text)

    def test_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "rachel_zane"
            inst.mkdir()
            cron = FakeCrontab("")
            install(
                inst,
                jc_binary="/usr/local/bin/jc",
                crontab_reader=cron.read,
                crontab_writer=cron.write,
            )
            result2 = install(
                inst,
                jc_binary="/usr/local/bin/jc",
                crontab_reader=cron.read,
                crontab_writer=cron.write,
            )
            self.assertFalse(result2["installed"])
            self.assertEqual(len(cron.writes), 1)

    def test_install_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "rachel_zane"
            inst.mkdir()
            cron = FakeCrontab("")
            result = install(
                inst,
                dry_run=True,
                jc_binary="/usr/local/bin/jc",
                crontab_reader=cron.read,
                crontab_writer=cron.write,
            )
            self.assertFalse(result["installed"])
            self.assertEqual(cron.writes, [])
            self.assertIn(BEGIN_MARKER, result["block"])

    def test_install_replaces_existing_block_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "rachel_zane"
            inst.mkdir()
            cron = FakeCrontab(
                "# header\n"
                f"{BEGIN_MARKER}rachel_zane ===\n"
                "*/9 * * * * stale\n"
                f"{END_MARKER}rachel_zane ===\n"
                "# tail\n"
            )
            install(
                inst,
                jc_binary="/usr/local/bin/jc",
                crontab_reader=cron.read,
                crontab_writer=cron.write,
            )
            self.assertIn("# header\n", cron.text)
            self.assertIn("# tail\n", cron.text)
            self.assertNotIn("*/9 * * * * stale", cron.text)
            self.assertIn("*/2 * * * *", cron.text)


class VerifyTests(unittest.TestCase):
    def test_verify_fails_when_block_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "rachel_zane"
            inst.mkdir()
            cron = FakeCrontab("# user line\n")
            f = verify(inst, crontab_reader=cron.read)
            self.assertEqual(f.level, "fail")
            self.assertIn("missing", f.message)

    def test_verify_ok_when_block_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "rachel_zane"
            inst.mkdir()
            cron = FakeCrontab("")
            install(
                inst,
                jc_binary="/usr/local/bin/jc",
                crontab_reader=cron.read,
                crontab_writer=cron.write,
            )
            f = verify(inst, crontab_reader=cron.read)
            self.assertEqual(f.level, "ok")

    def test_verify_fails_when_tick_cadence_wrong(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "rachel_zane"
            inst.mkdir()
            cron = FakeCrontab("")
            install(
                inst,
                jc_binary="/usr/local/bin/jc",
                tick_interval_minutes=5,
                crontab_reader=cron.read,
                crontab_writer=cron.write,
            )
            # Block was written with */5, verify asks for */2 → fail.
            f = verify(inst, tick_interval_minutes=2, crontab_reader=cron.read)
            self.assertEqual(f.level, "fail")
            self.assertIn("cadence", f.message)

    def test_verify_fails_when_reboot_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp) / "rachel_zane"
            inst.mkdir()
            block_no_reboot = (
                f"{BEGIN_MARKER}rachel_zane ===\n"
                "*/2 * * * * /usr/local/bin/jc watchdog tick --instance-dir /x\n"
                f"{END_MARKER}rachel_zane ===\n"
            )
            cron = FakeCrontab(block_no_reboot)
            f = verify(inst, crontab_reader=cron.read)
            self.assertEqual(f.level, "fail")
            self.assertIn("@reboot", f.message)


class EuidGuardTests(unittest.TestCase):
    """Root-contamination guard: install/verify refuse when the invoking euid
    doesn't own the instance dir (3 hosts contaminated 2026-06-05 by running
    the cron write from a root shell)."""

    def _instance(self, tmp: str) -> Path:
        inst = Path(tmp) / "rachel_zane"
        inst.mkdir()
        return inst

    def test_install_refuses_when_euid_differs_from_owner(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            inst = self._instance(tmp)
            cron = FakeCrontab("")
            wrong_euid = inst.stat().st_uid + 1
            with mock.patch("watchdog.install.os.geteuid", return_value=wrong_euid):
                with self.assertRaises(RuntimeError) as ctx:
                    install(
                        inst,
                        jc_binary="/usr/local/bin/jc",
                        crontab_reader=cron.read,
                        crontab_writer=cron.write,
                    )
            self.assertIn("does not own", str(ctx.exception))
            self.assertIn("su - <jc_user>", str(ctx.exception))
            # Nothing was written.
            self.assertEqual(cron.writes, [])

    def test_install_dry_run_also_refused(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            inst = self._instance(tmp)
            cron = FakeCrontab("")
            wrong_euid = inst.stat().st_uid + 1
            with mock.patch("watchdog.install.os.geteuid", return_value=wrong_euid):
                with self.assertRaises(RuntimeError):
                    install(
                        inst,
                        dry_run=True,
                        jc_binary="/usr/local/bin/jc",
                        crontab_reader=cron.read,
                        crontab_writer=cron.write,
                    )

    def test_verify_fails_loud_when_euid_differs(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            inst = self._instance(tmp)
            cron = FakeCrontab("")
            wrong_euid = inst.stat().st_uid + 1
            with mock.patch("watchdog.install.os.geteuid", return_value=wrong_euid):
                f = verify(inst, crontab_reader=cron.read)
            self.assertEqual(f.level, "fail")
            self.assertIn("does not own", f.message)

    def test_install_allowed_for_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            inst = self._instance(tmp)
            cron = FakeCrontab("")
            result = install(
                inst,
                jc_binary="/usr/local/bin/jc",
                crontab_reader=cron.read,
                crontab_writer=cron.write,
            )
            self.assertTrue(result["installed"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
