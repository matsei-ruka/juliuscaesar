"""OpenCode brain + adapter v2 — env, args, session capture, SQLite probe."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.brains.opencode import OPENCODE_ENV_KEYS, OpencodeBrain  # noqa: E402
from gateway.config import BrainOverrideConfig  # noqa: E402
from gateway.queue import Event  # noqa: E402


ADAPTER = REPO_ROOT / "lib" / "heartbeat" / "adapters" / "opencode.sh"


def _event(meta: dict | None = None) -> Event:
    return Event(
        id=1,
        source="telegram",
        source_message_id="m1",
        user_id="u1",
        conversation_id="c1",
        content="hello",
        meta=json.dumps(meta) if meta else None,
        status="queued",
        received_at="2026-06-07T00:00:00Z",
        available_at="2026-06-07T00:00:00Z",
        locked_by=None,
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


class OpencodeExtraEnvTests(unittest.TestCase):
    def test_returns_keys_from_instance_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text(
                "ANTHROPIC_API_KEY=ak\nOPENROUTER_API_KEY=ok\n", encoding="utf-8"
            )
            env = OpencodeBrain(instance).extra_env()
            self.assertEqual(env.get("ANTHROPIC_API_KEY"), "ak")
            self.assertEqual(env.get("OPENROUTER_API_KEY"), "ok")
            self.assertNotIn("OPENAI_API_KEY", env)

    def test_empty_when_env_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = OpencodeBrain(Path(tmp)).extra_env()
            for key in OPENCODE_ENV_KEYS:
                self.assertNotIn(key, env)

    def test_no_tools_override_exports_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain = OpencodeBrain(Path(tmp), override=BrainOverrideConfig(no_tools=True))
            self.assertEqual(brain.extra_env().get("JC_OPENCODE_NO_TOOLS"), "1")


class OpencodeExtraArgsTests(unittest.TestCase):
    def test_no_images_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = OpencodeBrain(Path(tmp)).extra_args_for_event(_event())
            self.assertEqual(args, ())

    def test_image_path_produces_file_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = OpencodeBrain(Path(tmp)).extra_args_for_event(
                _event({"image_path": "/tmp/a.png"})
            )
            self.assertEqual(args, ("--file", "/tmp/a.png"))

    def test_image_paths_list_produces_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = OpencodeBrain(Path(tmp)).extra_args_for_event(
                _event({"image_paths": ["/tmp/a.png", "/tmp/b.png"]})
            )
            self.assertEqual(args, ("--file", "/tmp/a.png", "--file", "/tmp/b.png"))

    def test_combined_image_path_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = OpencodeBrain(Path(tmp)).extra_args_for_event(
                _event({"image_path": "/tmp/a.png", "image_paths": ["/tmp/b.png"]})
            )
            self.assertEqual(args, ("--file", "/tmp/a.png", "--file", "/tmp/b.png"))


class OpencodeSessionFallbackTests(unittest.TestCase):
    def test_captures_session_with_numeric_created_timestamp(self) -> None:
        sessions = [
            {"id": "old", "created": 1778220000000, "directory": "/tmp/elsewhere"},
            {"id": "ses_new", "created": 1778226665499, "directory": "/tmp/instance"},
        ]
        proc = mock.Mock(returncode=0, stdout=json.dumps(sessions))
        with mock.patch("subprocess.run", return_value=proc):
            captured = OpencodeBrain(Path("/tmp/instance")).capture_session_id(
                "2026-05-08T07:51:00Z"
            )
        self.assertEqual(captured, "ses_new")

    def test_ignores_sessions_before_adapter_start(self) -> None:
        proc = mock.Mock(
            returncode=0,
            stdout=json.dumps([{"id": "old", "created": 1778220000000}]),
        )
        with mock.patch("subprocess.run", return_value=proc):
            captured = OpencodeBrain(Path("/tmp/instance")).capture_session_id(
                "2026-05-08T07:51:00Z"
            )
        self.assertIsNone(captured)

    def test_empty_list_returns_none(self) -> None:
        proc = mock.Mock(returncode=0, stdout="[]")
        with mock.patch("subprocess.run", return_value=proc):
            captured = OpencodeBrain(Path("/tmp/instance")).capture_session_id(
                "2026-05-08T07:51:00Z"
            )
        self.assertIsNone(captured)

    def test_opencode_missing_returns_none(self) -> None:
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            captured = OpencodeBrain(Path("/tmp/instance")).capture_session_id(
                "2026-05-08T07:51:00Z"
            )
        self.assertIsNone(captured)


def _make_opencode_db(path: Path, session_id: str, reply: str, tokens: dict) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    msg_id = "msg_test"
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        (
            msg_id,
            session_id,
            1,
            1,
            json.dumps({"role": "assistant", "tokens": tokens}),
        ),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        (
            "prt_text",
            msg_id,
            session_id,
            1,
            1,
            json.dumps({"type": "text", "text": reply}),
        ),
    )
    conn.commit()
    conn.close()


def _fake_opencode_script(stub_dir: Path, session_id: str) -> Path:
    path = stub_dir / "opencode"
    payload = json.dumps({"type": "step_start", "sessionID": session_id})
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            echo '{payload}'
            exit 0
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@unittest.skipUnless(ADAPTER.exists() and shutil.which("sqlite3"), "adapter or sqlite3 missing")
class OpencodeAdapterShellTests(unittest.TestCase):
    """Drive opencode.sh end-to-end with a stub `opencode` and a real SQLite DB."""

    def _run(self, *, db: Path, sidecar: Path, stub_dir: Path, prompt: str = "hi") -> subprocess.CompletedProcess:
        env = {
            "HOME": str(stub_dir),
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "OPENCODE_DB": str(db),
            "JC_USAGE_SIDECAR_PATH": str(sidecar),
        }
        return subprocess.run(
            [str(ADAPTER), "anthropic/claude-haiku-4.5"],
            input=prompt,
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )

    def test_writes_reply_to_stdout_and_sidecar_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "bin"
            stub.mkdir()
            db = Path(tmp) / "opencode.db"
            sidecar = Path(tmp) / "usage.json"
            session_id = "ses_test1"
            reply = "hello from db"
            tokens = {"input": 5, "output": 7, "cache": {"write": 100, "read": 200}}
            _make_opencode_db(db, session_id, reply, tokens)
            _fake_opencode_script(stub, session_id)

            res = self._run(db=db, sidecar=sidecar, stub_dir=stub)

            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertEqual(res.stdout, reply)
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload["session_id"], session_id)
            self.assertEqual(
                payload["usage"],
                {
                    "input_tokens": 5,
                    "output_tokens": 7,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 200,
                },
            )

    def test_db_missing_writes_error_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "bin"
            stub.mkdir()
            db = Path(tmp) / "absent.db"
            sidecar = Path(tmp) / "usage.json"
            _fake_opencode_script(stub, "ses_test2")

            res = self._run(db=db, sidecar=sidecar, stub_dir=stub)

            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout, "")
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("session_id"), "ses_test2")
            self.assertNotIn("usage", payload)


class OpencodeSidecarBaseReadTests(unittest.TestCase):
    """`Brain.invoke` reads $JC_USAGE_SIDECAR_PATH into BrainResult."""

    def test_sidecar_round_trip(self) -> None:
        from gateway.brains import base as base_module

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            payload = {
                "session_id": "ses_round_trip",
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 13,
                    "cache_creation_input_tokens": 17,
                    "cache_read_input_tokens": 19,
                },
            }
            stub_adapter = instance / "stub.sh"
            stub_adapter.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    cat > /dev/null
                    cat <<'JSON' > "$JC_USAGE_SIDECAR_PATH"
                    {json.dumps(payload)}
                    JSON
                    echo "reply body"
                    """
                ),
                encoding="utf-8",
            )
            stub_adapter.chmod(0o755)

            class _StubBrain(base_module.Brain):
                name = "opencode"
                needs_l1_preamble = False

                def adapter_path(self) -> Path:  # type: ignore[override]
                    return stub_adapter

                def prompt_for_event(self, event):  # type: ignore[override]
                    return event.content or ""

            log_path = instance / "gw.log"
            log_path.write_bytes(b"")
            event = _event()
            event_with_conv = event._replace(conversation_id=None) if hasattr(
                event, "_replace"
            ) else event
            result = _StubBrain(instance).invoke(
                event=event_with_conv,
                model=None,
                resume_session=None,
                timeout_seconds=10,
                log_path=log_path,
            )

            self.assertEqual(result.response, "reply body")
            self.assertEqual(result.session_id, "ses_round_trip")
            self.assertEqual(result.usage, payload["usage"])


if __name__ == "__main__":
    unittest.main()
