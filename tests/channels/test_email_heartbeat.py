"""Tests for the heartbeat email-poll integration.

Two layers:
1. The CLI entrypoint `python3 -m gateway.channels.email_dispatcher poll`
   wires fetch_new_messages → dispatch_messages with the right config
   block from gateway.yaml.
2. The bash wrapper `templates/init-instance/heartbeat/fetch/email-poll.sh`
   resolves paths, applies a flock, and shells out to the python entry.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.channels import email_dispatcher
from gateway import queue as queue_module


SCRIPT = REPO_ROOT / "templates" / "init-instance" / "heartbeat" / "fetch" / "email-poll.sh"


def _write_gateway_yaml(instance: Path, body: str) -> None:
    (instance / "ops").mkdir(parents=True, exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(body, encoding="utf-8")


class TestPollCli(unittest.TestCase):
    """Drive the `poll` subcommand directly without bash."""

    def _yaml(self) -> str:
        return textwrap.dedent(
            """
            channels:
              email:
                enabled: true
                imap:
                  host: mail.example.com
                  user: mario@scovai.com
                  poll_interval: 0
                senders:
                  trusted:
                    - mario@scovai.com
                  blocklist:
                    - spam@x.com
                notify_on_unknown: false
            """
        ).strip()

    def test_poll_dispatches_trusted_via_adapter_mock(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway_yaml(instance, self._yaml())

            fake_msgs = [
                {
                    "channel": "email",
                    "channel_id": "uid_1",
                    "conversation_id": "email_mario@scovai.com",
                    "user_id": "email_mario@scovai.com",
                    "sender": "mario@scovai.com",
                    "sender_name": "Mario",
                    "subject": "Test",
                    "message_id": "<1@x.com>",
                    "in_reply_to": None,
                    "references": [],
                    "text": "[EMAIL ...] hello",
                    "status": "trusted",
                    "metadata": {"uid": "1"},
                },
                {
                    "channel": "email",
                    "channel_id": "uid_2",
                    "conversation_id": "email_spam@x.com",
                    "user_id": "email_spam@x.com",
                    "sender": "spam@x.com",
                    "sender_name": "Spam",
                    "subject": "Buy",
                    "message_id": "<2@x.com>",
                    "in_reply_to": None,
                    "references": [],
                    "text": "spam",
                    "status": "blocked",
                    "metadata": {"uid": "2"},
                },
            ]
            fake_adapter = MagicMock()
            fake_adapter.fetch_new_messages.return_value = fake_msgs

            with patch.dict(
                "sys.modules",
                {
                    "channels.email": MagicMock(
                        EmailChannelAdapter=MagicMock(return_value=fake_adapter)
                    )
                },
            ):
                # dotenv may be missing on some test envs — stub if so.
                try:
                    import dotenv  # noqa: F401
                except ImportError:
                    sys.modules["dotenv"] = MagicMock(load_dotenv=lambda *_a, **_k: None)
                argv = ["poll", "--instance-dir", str(instance)]
                rc = email_dispatcher.main(argv)
            self.assertEqual(rc, 0)
            fake_adapter.fetch_new_messages.assert_called_once()

            conn = queue_module.connect(instance)
            try:
                rows = conn.execute(
                    "SELECT source, source_message_id FROM events"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(len(rows), 1)  # blocked dropped, trusted enqueued
            self.assertEqual(rows[0]["source_message_id"], "uid_1")
            fake_adapter.mark_handled_uids.assert_called_once_with(["1", "2"])

            # Poll log was created.
            log = instance / "state" / "channels" / "email" / "poll.log"
            self.assertTrue(log.exists())
            self.assertIn("dispatched=1", log.read_text())

    def test_poll_skips_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway_yaml(
                instance,
                "channels:\n  email:\n    enabled: false\n",
            )
            try:
                import dotenv  # noqa: F401
            except ImportError:
                sys.modules["dotenv"] = MagicMock(load_dotenv=lambda *_a, **_k: None)
            rc = email_dispatcher.main(["poll", "--instance-dir", str(instance)])
            self.assertEqual(rc, 0)


@unittest.skipIf(
    shutil.which("flock") is None or shutil.which("bash") is None,
    "needs bash + flock",
)
class TestShellWrapper(unittest.TestCase):
    """Drive the bash wrapper end-to-end with a stubbed framework lib."""

    def test_wrapper_invokes_python_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            (instance / "heartbeat" / "fetch").mkdir(parents=True)
            (instance / "ops").mkdir()
            (instance / "ops" / "gateway.yaml").write_text(
                "channels:\n  email:\n    enabled: false\n", encoding="utf-8"
            )
            shutil.copy2(SCRIPT, instance / "heartbeat" / "fetch" / "email-poll.sh")

            # Stand up a stub framework lib that supplies a fake gateway module
            # which prints a marker and exits 0 for `poll --instance-dir <p>`.
            stub_root = Path(tmp) / "stub-framework"
            stub_lib = stub_root / "lib"
            (stub_lib / "gateway" / "channels").mkdir(parents=True)
            (stub_lib / "gateway" / "__init__.py").write_text("", encoding="utf-8")
            (stub_lib / "gateway" / "channels" / "__init__.py").write_text(
                "", encoding="utf-8"
            )
            (stub_lib / "gateway" / "channels" / "email_dispatcher.py").write_text(
                textwrap.dedent(
                    f"""
                    import sys
                    if __name__ == "__main__" and len(sys.argv) >= 4 and sys.argv[1] == "poll":
                        marker = "{instance}/poll-was-called"
                        with open(marker, "w") as f:
                            f.write(" ".join(sys.argv))
                        sys.exit(0)
                    """
                ).strip(),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["JC_FRAMEWORK_DIR"] = str(stub_root)

            proc = subprocess.run(
                ["bash", str(instance / "heartbeat" / "fetch" / "email-poll.sh")],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            marker = instance / "poll-was-called"
            self.assertTrue(marker.exists(), proc.stderr)
            # Lock dir + log created.
            self.assertTrue((instance / "state" / "channels" / "email").is_dir())

    def test_wrapper_lock_prevents_overlap(self):
        """If the lock is already held, the second invocation exits 0 quickly
        without running the python module again."""
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            (instance / "heartbeat" / "fetch").mkdir(parents=True)
            (instance / "state" / "channels" / "email").mkdir(parents=True)
            (instance / "ops").mkdir()
            (instance / "ops" / "gateway.yaml").write_text(
                "channels:\n  email:\n    enabled: false\n", encoding="utf-8"
            )
            shutil.copy2(SCRIPT, instance / "heartbeat" / "fetch" / "email-poll.sh")

            stub_root = Path(tmp) / "stub-framework"
            stub_lib = stub_root / "lib"
            (stub_lib / "gateway" / "channels").mkdir(parents=True)
            (stub_lib / "gateway" / "__init__.py").write_text("", encoding="utf-8")
            (stub_lib / "gateway" / "channels" / "__init__.py").write_text(
                "", encoding="utf-8"
            )
            counter_file = instance / "call-count"
            counter_file.write_text("0", encoding="utf-8")
            (stub_lib / "gateway" / "channels" / "email_dispatcher.py").write_text(
                textwrap.dedent(
                    f"""
                    import sys, time
                    if __name__ == "__main__":
                        with open("{counter_file}", "r") as f:
                            n = int(f.read())
                        with open("{counter_file}", "w") as f:
                            f.write(str(n + 1))
                        time.sleep(2)
                        sys.exit(0)
                    """
                ).strip(),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["JC_FRAMEWORK_DIR"] = str(stub_root)
            script = instance / "heartbeat" / "fetch" / "email-poll.sh"

            p1 = subprocess.Popen(
                ["bash", str(script)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # Brief settle so p1 grabs the lock before p2 tries.
            import time
            time.sleep(0.3)
            p2 = subprocess.run(
                ["bash", str(script)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            p1.communicate(timeout=30)

            self.assertEqual(p2.returncode, 0)
            # Counter incremented exactly once (p1 ran; p2 saw lock and bailed).
            self.assertEqual(counter_file.read_text(), "1")


if __name__ == "__main__":
    unittest.main()
