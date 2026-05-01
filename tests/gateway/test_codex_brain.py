"""Tests for Codex gateway brain sandbox and adapter arguments."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.brains.codex import CodexBrain  # noqa: E402
from gateway.config import BrainOverrideConfig, ConfigError, load_config  # noqa: E402
from gateway.queue import Event  # noqa: E402


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
        received_at="2026-05-01T00:00:00Z",
        available_at="2026-05-01T00:00:00Z",
        locked_by=None,
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


def _write_gateway_config(instance: Path, text: str) -> None:
    (instance / ".jc").write_text("", encoding="utf-8")
    (instance / "ops").mkdir()
    (instance / "ops" / "gateway.yaml").write_text(text, encoding="utf-8")


class CodexBrainSandboxTests(unittest.TestCase):
    def test_unset_codex_sandbox_defaults_to_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain = CodexBrain(Path(tmp), override=BrainOverrideConfig())
            self.assertEqual(brain.extra_env()["CODEX_SANDBOX"], "read-only")

    def test_explicit_sandbox_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain = CodexBrain(
                Path(tmp),
                override=BrainOverrideConfig(sandbox="workspace-write"),
            )
            self.assertEqual(brain.extra_env()["CODEX_SANDBOX"], "workspace-write")

    def test_yolo_maps_to_dangerous_sandbox_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain = CodexBrain(
                Path(tmp),
                override=BrainOverrideConfig(yolo=True),
            )
            self.assertEqual(brain.extra_env()["CODEX_SANDBOX"], "yolo")

    def test_image_path_metadata_adds_image_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain = CodexBrain(Path(tmp), override=BrainOverrideConfig())
            args = brain.extra_args_for_event(
                _event(
                    {
                        "image_path": "/tmp/one.png",
                        "image_paths": ["/tmp/two.png"],
                    }
                )
            )
            self.assertEqual(
                args,
                ("--image", "/tmp/one.png", "--image", "/tmp/two.png"),
            )


class CodexConfigValidationTests(unittest.TestCase):
    def test_yolo_conflicting_sandbox_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway_config(
                instance,
                "default_brain: codex\n"
                "brains:\n"
                "  codex:\n"
                "    yolo: true\n"
                "    sandbox: read-only\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("yolo=true conflicts", str(ctx.exception))

    def test_invalid_codex_sandbox_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway_config(
                instance,
                "default_brain: codex\n"
                "brains:\n"
                "  codex:\n"
                "    sandbox: moon-base\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("brains.codex.sandbox", str(ctx.exception))


class CodexShellAdapterTests(unittest.TestCase):
    def _run_adapter(
        self,
        *,
        sandbox: str | None = None,
        model: str = "gpt-5",
        extra_args: list[str] | None = None,
    ) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            argv_file = root / "argv.txt"
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$CODEX_ARGV_FILE\"\n"
                "cat >/dev/null\n"
                "printf 'fake ok\\n'\n",
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_ARGV_FILE"] = str(argv_file)
            if sandbox is None:
                env.pop("CODEX_SANDBOX", None)
            else:
                env["CODEX_SANDBOX"] = sandbox

            proc = subprocess.run(
                [
                    str(REPO_ROOT / "lib" / "heartbeat" / "adapters" / "codex.sh"),
                    model,
                    *(extra_args or []),
                ],
                input="prompt",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=True,
            )
            self.assertIn("fake ok", proc.stdout)
            return argv_file.read_text(encoding="utf-8").splitlines()

    def test_default_argv_uses_supported_read_only_sandbox(self) -> None:
        argv = self._run_adapter()
        self.assertEqual(argv, ["exec", "--sandbox", "read-only", "--model", "gpt-5", "-"])
        self.assertNotIn("--ask-for-approval", argv)

    def test_image_arg_is_passed_before_prompt_dash(self) -> None:
        argv = self._run_adapter(extra_args=["--image", "/tmp/scan.png"])
        self.assertEqual(
            argv,
            [
                "exec",
                "--sandbox",
                "read-only",
                "--model",
                "gpt-5",
                "--image",
                "/tmp/scan.png",
                "-",
            ],
        )

    def test_yolo_argv_uses_supported_dangerous_flag(self) -> None:
        argv = self._run_adapter(sandbox="yolo")
        self.assertEqual(
            argv,
            [
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                "gpt-5",
                "-",
            ],
        )


if __name__ == "__main__":
    unittest.main()
