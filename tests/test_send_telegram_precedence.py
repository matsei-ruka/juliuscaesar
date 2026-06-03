"""Table-driven tests for send_telegram.py chat_id precedence ladder.

Locks the contract from docs/specs/origin-chat-id.md:

  --chat-id
    > $TELEGRAM_CHAT_ID_OVERRIDE
    > $ORIGIN_CHAT_ID
    > $TELEGRAM_CHAT_ID      (DEPRECATED — stderr warning, one release)
    > TELEGRAM_CHAT_ID in .env (DEPRECATED — stderr warning, one release)
    > error (verbose _format_no_chat_id_error block)

The deprecated branches emit a one-shot stderr line so callers surface
in logs before removal in the next release.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

PY_SENDER = REPO_ROOT / "lib" / "heartbeat" / "lib" / "send_telegram.py"


def _load_sender_module():
    spec = importlib.util.spec_from_file_location("send_telegram_prec", PY_SENDER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


CLEAR_KEYS = (
    "TELEGRAM_CHAT_ID_OVERRIDE",
    "ORIGIN_CHAT_ID",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_BOT_TOKEN",
    "JC_INSTANCE_DIR",
    "JC_PUSH_MARKER_PATH",
)


class PrecedenceLadderTests(unittest.TestCase):
    """Run main() for each ladder path, assert the right chat_id is used.

    All five winning paths + one losing path (no source resolves → error).
    The deprecated paths additionally assert the stderr warning fires.
    """

    def setUp(self) -> None:
        self.mod = _load_sender_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.instance = Path(self.tmp.name)
        (self.instance / ".jc").write_text("", encoding="utf-8")
        (self.instance / "memory" / "L1").mkdir(parents=True)
        (self.instance / "memory" / "L1" / "CHATS.md").write_text(
            "28547271 | private | Luca\n", encoding="utf-8"
        )
        self.posted: list[dict] = []

        def fake_post(url, payload, *, timeout=20):
            self.posted.append(dict(payload))
            return 200, {"ok": True, "result": {"message_id": 1}}

        self._post_patch = mock.patch.object(self.mod, "_post", side_effect=fake_post)
        self._post_patch.start()

    def tearDown(self) -> None:
        self._post_patch.stop()
        self.tmp.cleanup()

    def _run(
        self,
        *,
        cli_chat_id: str | None,
        env_extras: dict[str, str],
        argv: list[str] | None = None,
        write_env_chat_id: str | None = None,
    ) -> tuple[int, str]:
        """Drive main() in isolation. Returns (rc, stderr_text)."""
        env_file = self.instance / ".env"
        env_lines = ["TELEGRAM_BOT_TOKEN=test-token"]
        if write_env_chat_id is not None:
            env_lines.append(f"TELEGRAM_CHAT_ID={write_env_chat_id}")
        env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

        base_env = {"JC_INSTANCE_DIR": str(self.instance)}
        base_env.update(env_extras)

        cli_argv = ["send_telegram"]
        if cli_chat_id is not None:
            cli_argv += ["--chat-id", cli_chat_id]
        if argv:
            cli_argv += argv

        stderr_buf = io.StringIO()

        with mock.patch.dict(os.environ, base_env, clear=True), \
             mock.patch.object(sys, "stdin", io.StringIO("body")), \
             mock.patch.object(sys, "stdout", io.StringIO()), \
             mock.patch.object(sys, "stderr", stderr_buf), \
             mock.patch.object(sys, "argv", cli_argv):
            rc = self.mod.main()
        return rc, stderr_buf.getvalue()

    def test_path_1_cli_flag_wins_over_everything(self):
        rc, stderr = self._run(
            cli_chat_id="111",
            env_extras={
                "TELEGRAM_CHAT_ID_OVERRIDE": "222",
                "ORIGIN_CHAT_ID": "333",
                "TELEGRAM_CHAT_ID": "444",
            },
            write_env_chat_id="555",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.posted[-1]["chat_id"], "111")
        self.assertNotIn("DEPRECATED", stderr)

    def test_path_2_override_env_wins_over_origin_and_legacy(self):
        rc, stderr = self._run(
            cli_chat_id=None,
            env_extras={
                "TELEGRAM_CHAT_ID_OVERRIDE": "222",
                "ORIGIN_CHAT_ID": "333",
                "TELEGRAM_CHAT_ID": "444",
            },
            write_env_chat_id="555",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.posted[-1]["chat_id"], "222")
        self.assertNotIn("DEPRECATED", stderr)

    def test_path_3_origin_chat_id_wins_over_legacy(self):
        rc, stderr = self._run(
            cli_chat_id=None,
            env_extras={
                "ORIGIN_CHAT_ID": "333",
                "TELEGRAM_CHAT_ID": "444",
            },
            write_env_chat_id="555",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.posted[-1]["chat_id"], "333")
        self.assertNotIn("DEPRECATED", stderr)

    def test_path_4_telegram_chat_id_env_var_warns_and_sends(self):
        rc, stderr = self._run(
            cli_chat_id=None,
            env_extras={"TELEGRAM_CHAT_ID": "444"},
            write_env_chat_id="555",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.posted[-1]["chat_id"], "444")
        self.assertIn("DEPRECATED", stderr)
        self.assertIn("$TELEGRAM_CHAT_ID env var", stderr)

    def test_path_5_env_file_telegram_chat_id_warns_and_sends(self):
        rc, stderr = self._run(
            cli_chat_id=None,
            env_extras={},
            write_env_chat_id="555",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.posted[-1]["chat_id"], "555")
        self.assertIn("DEPRECATED", stderr)
        self.assertIn(".env", stderr)

    def test_path_6_nothing_resolves_emits_verbose_error_and_exits_nonzero(self):
        rc, stderr = self._run(
            cli_chat_id=None,
            env_extras={},
            write_env_chat_id=None,
        )
        self.assertEqual(rc, 1)
        self.assertIn("[send_telegram] ERROR: no chat_id resolved", stderr)
        self.assertIn("--chat-id (none)", stderr)
        self.assertIn("TELEGRAM_CHAT_ID_OVERRIDE (unset)", stderr)
        self.assertIn("ORIGIN_CHAT_ID (unset)", stderr)
        # CHATS.md content surfaced
        self.assertIn("28547271 | private | Luca", stderr)
        # Fix-it hints surface every path
        self.assertIn("Inbound event reply", stderr)
        self.assertIn("HB / cron task", stderr)
        self.assertIn("Manual one-off", stderr)
        # No actual send happened
        self.assertEqual(self.posted, [])

    def test_verbose_error_omits_known_chats_block_when_missing(self):
        (self.instance / "memory" / "L1" / "CHATS.md").unlink()
        rc, stderr = self._run(
            cli_chat_id=None,
            env_extras={},
            write_env_chat_id=None,
        )
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: no chat_id resolved", stderr)
        self.assertNotIn("Known chats", stderr)


if __name__ == "__main__":
    unittest.main()
