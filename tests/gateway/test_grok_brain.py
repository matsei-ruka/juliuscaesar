"""Grok brain + adapter — args, env, NDJSON parse, sidecar token telemetry.

Covers the six cases enumerated in docs/specs/grok-adapter.md §7 plus the
brain spec alias resolution that lib/heartbeat/adapters/grok.sh performs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.brains.aliases import resolve_alias  # noqa: E402
from gateway.brains.grok import GROK_ENV_KEYS, GrokBrain  # noqa: E402
from gateway.config import BrainOverrideConfig  # noqa: E402
from gateway.queue import Event  # noqa: E402


ADAPTER = REPO_ROOT / "lib" / "heartbeat" / "adapters" / "grok.sh"


def _event(meta: dict | None = None, *, conversation_id: str = "c1") -> Event:
    return Event(
        id=1,
        source="telegram",
        source_message_id="m1",
        user_id="u1",
        conversation_id=conversation_id,
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


class GrokExtraEnvTests(unittest.TestCase):
    """API keys for grok come from the instance .env, not os.environ."""

    def test_xai_api_key_pulled_from_instance_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text("XAI_API_KEY=xak\n", encoding="utf-8")
            env = GrokBrain(instance).extra_env()
            self.assertEqual(env.get("XAI_API_KEY"), "xak")

    def test_empty_when_env_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = GrokBrain(Path(tmp)).extra_env()
            for key in GROK_ENV_KEYS:
                self.assertNotIn(key, env)


class GrokExtraArgsTests(unittest.TestCase):
    """``extra_args_for_event`` builds the static + dynamic CLI tail."""

    def test_system_prompt_override_is_present_when_l1_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = GrokBrain(Path(tmp)).extra_args_for_event(_event())
        self.assertIn("--system-prompt-override", args)
        idx = args.index("--system-prompt-override")
        # The next arg should be a non-empty string (the rendered preamble).
        self.assertIsInstance(args[idx + 1], str)
        self.assertGreater(len(args[idx + 1]), 0)

    def test_always_approve_and_streaming_json_flags_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = GrokBrain(Path(tmp)).extra_args_for_event(_event())
        self.assertIn("--always-approve", args)
        self.assertIn("--output-format", args)
        self.assertEqual(
            args[args.index("--output-format") + 1], "streaming-json"
        )

    def test_image_path_appends_file_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = GrokBrain(Path(tmp)).extra_args_for_event(
                _event({"image_path": "/tmp/a.png"})
            )
        self.assertIn("--file", args)
        self.assertEqual(
            args[args.index("--file") + 1], "/tmp/a.png"
        )

    def test_image_paths_list_produces_multiple_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = GrokBrain(Path(tmp)).extra_args_for_event(
                _event({"image_paths": ["/tmp/a.png", "/tmp/b.png"]})
            )
        joined = list(args)
        self.assertEqual(joined.count("--file"), 2)


class GrokBrainSpecAliasTests(unittest.TestCase):
    """Brain spec aliases resolve at the gateway short-name layer."""

    def test_grok_default_alias(self) -> None:
        self.assertEqual(resolve_alias("grok"), "grok")

    def test_grok_fast_alias(self) -> None:
        self.assertEqual(resolve_alias("grok-fast"), "grok:fast")

    def test_grok_build_alias(self) -> None:
        self.assertEqual(resolve_alias("grok-build"), "grok:grok-build")


def _fake_grok_script(stub_dir: Path, session_id: str, reply: str) -> Path:
    """Write a stub grok binary that emits the three-event NDJSON."""
    path = stub_dir / "grok"
    events = [
        {"type": "thought", "data": "reasoning to be ignored"},
        {"type": "text", "data": reply[: len(reply) // 2]},
        {"type": "text", "data": reply[len(reply) // 2 :]},
        {
            "type": "end",
            "stopReason": "EndTurn",
            "sessionId": session_id,
            "requestId": "req_test",
        },
    ]
    payload = "\n".join(json.dumps(ev) for ev in events)
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            cat <<'NDJSON'
            {payload}
            NDJSON
            exit 0
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _seed_updates_jsonl(home_dir: Path, cwd: Path, session_id: str, tokens: int) -> None:
    """Mirror the on-disk layout grok writes after a turn so the probe finds it."""
    slug = urllib.parse.quote(str(cwd), safe="")
    session_dir = home_dir / ".grok" / "sessions" / slug / session_id
    session_dir.mkdir(parents=True)
    payload = {
        "type": "update",
        "_meta": {
            "totalTokens": {
                "effective_input_tokens": tokens,
            },
        },
    }
    (session_dir / "updates.jsonl").write_text(
        json.dumps(payload) + "\n", encoding="utf-8"
    )


@unittest.skipUnless(ADAPTER.exists(), "adapter missing")
class GrokAdapterShellTests(unittest.TestCase):
    """End-to-end: stub grok, real adapter, real sidecar + updates.jsonl probe."""

    def _run(
        self,
        *,
        stub_dir: Path,
        sidecar: Path,
        prompt: str = "hi",
        model: str = "",
        env_extra: dict | None = None,
    ) -> subprocess.CompletedProcess:
        env = {
            "HOME": str(stub_dir),
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "JC_USAGE_SIDECAR_PATH": str(sidecar),
        }
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [str(ADAPTER), model],
            input=prompt,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(stub_dir),
            timeout=20,
        )

    def test_reply_and_session_captured_from_ndjson(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stub = tmp_path / "bin"
            stub.mkdir()
            session_id = "019ea13c-0000-7000-8000-000000000001"
            reply = "hello from grok"
            _fake_grok_script(stub, session_id, reply)
            _seed_updates_jsonl(tmp_path, stub, session_id, tokens=4242)
            sidecar = tmp_path / "usage.json"

            # The adapter probes ~/$HOME/.grok/sessions/<urlencode(cwd)>/<sid>/
            # so HOME must point at the temp dir AND cwd must be the temp dir.
            env_extra = {"HOME": str(tmp_path)}
            res = self._run(
                stub_dir=stub, sidecar=sidecar, env_extra=env_extra
            )

            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertEqual(res.stdout, reply)
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload["session_id"], session_id)
            self.assertEqual(payload["usage"]["input_tokens"], 4242)
            self.assertEqual(payload["usage"]["output_tokens"], 0)

    def test_resume_flag_added_when_env_set(self) -> None:
        # Stub grok writes its argv into a file so we can assert -r presence.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stub = tmp_path / "bin"
            stub.mkdir()
            argv_log = tmp_path / "argv.txt"
            (stub / "grok").write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    printf '%s\\n' "$@" > {argv_log}
                    echo '{{"type":"end","stopReason":"EndTurn","sessionId":"sid_resume","requestId":"r"}}'
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            (stub / "grok").chmod(0o755)
            sidecar = tmp_path / "usage.json"
            res = self._run(
                stub_dir=stub,
                sidecar=sidecar,
                env_extra={
                    "HOME": str(tmp_path),
                    "JC_RESUME_SESSION": "sid_prior",
                },
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            argv = argv_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("-r", argv)
            self.assertEqual(argv[argv.index("-r") + 1], "sid_prior")

    def test_no_resume_flag_for_fresh_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stub = tmp_path / "bin"
            stub.mkdir()
            argv_log = tmp_path / "argv.txt"
            (stub / "grok").write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    printf '%s\\n' "$@" > {argv_log}
                    echo '{{"type":"end","stopReason":"EndTurn","sessionId":"sid_fresh","requestId":"r"}}'
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            (stub / "grok").chmod(0o755)
            sidecar = tmp_path / "usage.json"
            res = self._run(
                stub_dir=stub,
                sidecar=sidecar,
                env_extra={"HOME": str(tmp_path)},
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            argv = argv_log.read_text(encoding="utf-8").splitlines()
            self.assertNotIn("-r", argv)


if __name__ == "__main__":
    unittest.main()
