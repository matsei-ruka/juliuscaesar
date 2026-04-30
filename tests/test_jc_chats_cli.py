"""Smoke tests for the jc-chats CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import chats  # noqa: E402


CLI = str(REPO_ROOT / "bin" / "jc-chats")


def _run(args: list[str], instance: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, CLI, "--instance-dir", str(instance), *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


class JcChatsCliTests(unittest.TestCase):
    def _seed(self, instance: Path) -> None:
        chats.upsert_chat(
            instance,
            channel="telegram",
            chat_id="42",
            chat_type="private",
            title="Luca Mattei",
            username="luca",
            last_message_id="1",
        )
        chats.upsert_chat(
            instance,
            channel="telegram",
            chat_id="-100",
            chat_type="supergroup",
            title="BNESIM ops",
            member_count=8,
            last_message_id="2",
        )

    def test_list_json_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._seed(instance)
            rc, out, err = _run(["list", "--json"], instance)
            self.assertEqual(rc, 0, err)
            data = json.loads(out)
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 2)
            row = data[0]
            for key in (
                "channel",
                "chat_id",
                "chat_type",
                "title",
                "username",
                "member_count",
                "first_seen",
                "last_seen",
                "last_message_id",
            ):
                self.assertIn(key, row)

    def test_list_filters_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._seed(instance)
            chats.upsert_chat(instance, channel="discord", chat_id="X", title="X")
            rc, out, _ = _run(["list", "--channel", "telegram", "--json"], instance)
            self.assertEqual(rc, 0)
            ids = [c["chat_id"] for c in json.loads(out)]
            self.assertEqual(set(ids), {"42", "-100"})

    def test_list_human_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._seed(instance)
            rc, out, _ = _run(["list"], instance)
            self.assertEqual(rc, 0)
            self.assertIn("Luca Mattei", out)
            self.assertIn("BNESIM ops", out)
            self.assertIn("CHAT_ID", out)

    def test_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._seed(instance)
            rc, out, _ = _run(["show", "42", "--json"], instance)
            self.assertEqual(rc, 0)
            data = json.loads(out)
            self.assertEqual(data["chat_id"], "42")
            self.assertEqual(data["title"], "Luca Mattei")

    def test_show_missing_chat_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            rc, _, err = _run(["show", "999"], instance)
            self.assertNotEqual(rc, 0)
            self.assertIn("not found", err)

    def test_prune_dry_run_lists_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._seed(instance)
            # Age the supergroup row.
            from gateway import queue

            conn = queue.connect(instance)
            try:
                conn.execute(
                    "UPDATE chats SET last_seen='2020-01-01T00:00:00Z' "
                    "WHERE chat_id='-100'"
                )
                conn.commit()
            finally:
                conn.close()
            rc, out, _ = _run(["prune", "--older-than", "30d"], instance)
            self.assertEqual(rc, 0)
            self.assertIn("would delete 1", out)
            # Row still present (no --yes).
            still = chats.get_chat(instance, channel="telegram", chat_id="-100")
            self.assertIsNotNone(still)

    def test_prune_with_yes_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._seed(instance)
            from gateway import queue

            conn = queue.connect(instance)
            try:
                conn.execute(
                    "UPDATE chats SET last_seen='2020-01-01T00:00:00Z' "
                    "WHERE chat_id='-100'"
                )
                conn.commit()
            finally:
                conn.close()
            rc, out, _ = _run(
                ["prune", "--older-than", "30d", "--yes"], instance
            )
            self.assertEqual(rc, 0)
            self.assertIn("removed 1", out)
            self.assertIsNone(
                chats.get_chat(instance, channel="telegram", chat_id="-100")
            )


class JcChatsAuthCliTests(unittest.TestCase):
    def _seed_with_pending(self, instance: Path) -> None:
        chats.upsert_chat(
            instance,
            channel="telegram",
            chat_id="-100",
            chat_type="supergroup",
            title="BNESIM ops",
            auth_status="pending",
        )
        chats.upsert_chat(
            instance,
            channel="telegram",
            chat_id="42",
            chat_type="private",
            title="Luca",
            auth_status="allowed",
        )

    def test_list_filter_by_auth_status_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._seed_with_pending(instance)
            rc, out, _ = _run(
                ["list", "--auth-status", "pending", "--json"], instance
            )
            self.assertEqual(rc, 0)
            data = json.loads(out)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["chat_id"], "-100")

    def test_approve_writes_yaml_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._seed_with_pending(instance)
            rc, out, _ = _run(["approve", "-100"], instance)
            self.assertEqual(rc, 0)
            self.assertIn("ops/gateway.yaml updated", out)
            self.assertIn(".env updated", out)
            from gateway.config import load_config
            from gateway.config_writer import env_chat_ids
            cfg = load_config(instance).channel("telegram")
            self.assertIn("-100", cfg.chat_ids)
            self.assertIn("-100", env_chat_ids(instance))

    def test_deny_writes_blocklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._seed_with_pending(instance)
            rc, out, _ = _run(["deny", "-100"], instance)
            self.assertEqual(rc, 0)
            self.assertIn("blocked_chat_ids", out)
            from gateway.config import load_config
            cfg = load_config(instance).channel("telegram")
            self.assertIn("-100", cfg.blocked_chat_ids)

    def test_approve_unknown_chat_succeeds(self):
        """CLI no longer requires a DB row — config-only authority.

        The bot may not have seen the chat yet (operator pre-approving
        a known id), so we don't reject; we just write config.
        """
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            rc, _out, _err = _run(["approve", "999"], instance)
            self.assertEqual(rc, 0)
            from gateway.config import load_config
            cfg = load_config(instance).channel("telegram")
            self.assertIn("999", cfg.chat_ids)

    def test_approve_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "ops").mkdir(exist_ok=True)
            (instance / "ops" / "gateway.yaml").write_text(
                "channels:\n  telegram:\n    chat_ids: [-100]\n"
            )
            (instance / ".env").write_text("TELEGRAM_CHAT_IDS='-100'\n")
            rc, out, _ = _run(["approve", "-100"], instance)
            self.assertEqual(rc, 0)
            self.assertIn("already approved", out)

    def test_migrate_to_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            chats.upsert_chat(
                instance, channel="telegram", chat_id="-100",
                auth_status="allowed",
            )
            chats.upsert_chat(
                instance, channel="telegram", chat_id="-200",
                auth_status="denied",
            )
            rc, out, _ = _run(["migrate-to-config"], instance)
            self.assertEqual(rc, 0)
            from gateway.config import load_config
            cfg = load_config(instance).channel("telegram")
            self.assertIn("-100", cfg.chat_ids)
            self.assertIn("-200", cfg.blocked_chat_ids)


if __name__ == "__main__":
    unittest.main()
