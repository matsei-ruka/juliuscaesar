"""Tests for pi.dev brain wrapper and session capture.

Covers docs/specs/pi-brain.md Phase 2 acceptance:

- PiBrain class identity and preamble flag.
- pre_invoke_snapshot returns frozenset; handles missing dir.
- capture_session_id returns UUID from <ts>_<uuid>.jsonl filename.
- capture_session_id returns None when no new file / multiple new files /
  filename doesn't match pattern.
- extra_env injects API keys and JC_PI_NO_TOOLS.
- extra_args_for_event returns --thinking when config set.
- prompt_for_event contains the gateway output contract.
- _pi_session_dir produces correct slug from instance path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.brains.pi import (  # noqa: E402
    PiBrain,
    _pi_session_dir,
    _session_has_image_url,
    _snapshot_session_paths,
    _PI_SESSION_UUID_RE,
)
from gateway.config import BrainOverrideConfig, env_value  # noqa: E402
from gateway.queue import Event  # noqa: E402


def _event(**kwargs) -> Event:
    defaults = {
        "id": 1,
        "source": "telegram",
        "source_message_id": "m1",
        "user_id": "u1",
        "conversation_id": "c1",
        "content": "hello",
        "meta": None,
        "status": "queued",
        "received_at": "2026-05-14T00:00:00Z",
        "available_at": "2026-05-14T00:00:00Z",
        "locked_by": None,
        "locked_until": None,
        "started_at": None,
        "finished_at": None,
        "retry_count": 0,
        "response": None,
        "error": None,
    }
    defaults.update(kwargs)
    return Event(**defaults)


class _PiHome:
    """Context manager that points pi sessions at a temp directory.

    Overrides HOME so ~/.pi/agent/sessions/ resolves to a temp dir.
    Creates the slug subdirectory matching the given cwd.
    """

    def __init__(self, cwd: str):
        self._cwd = cwd
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        # Patches must be started before computing _pi_session_dir,
        # because it uses Path.home() and os.path.realpath internally.
        self._home_patch = mock.patch.object(Path, "home", return_value=self.home)
        self._realpath_patch = mock.patch(
            "os.path.realpath", return_value=cwd
        )

    def __enter__(self):
        self._home_patch.start()
        self._realpath_patch.start()
        self.sessions = _pi_session_dir(self._cwd)
        self.sessions.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        self._realpath_patch.stop()
        self._home_patch.stop()
        self._tmp.cleanup()

    def write_session(self, name: str) -> Path:
        path = self.sessions / name
        path.write_text("{}", encoding="utf-8")
        return path


class PiBrainIdentityTests(unittest.TestCase):
    def test_name_is_pi(self) -> None:
        self.assertEqual(PiBrain.name, "pi")

    def test_needs_l1_preamble_is_true(self) -> None:
        self.assertTrue(PiBrain.needs_l1_preamble)


class PiSessionDirTests(unittest.TestCase):
    def test_slug_derivation(self) -> None:
        """Verify _pi_session_dir produces the expected pi slug format."""
        with mock.patch.object(Path, "home", return_value=Path("/home/user")):
            with mock.patch("os.path.realpath", return_value="/home/user/my-instance"):
                result = _pi_session_dir("/home/user/my-instance")
                self.assertEqual(
                    result,
                    Path("/home/user/.pi/agent/sessions/--home-user-my-instance--"),
                )

    def test_slug_resolves_symlinks(self) -> None:
        """realpath is used, not the raw cwd."""
        with mock.patch.object(Path, "home", return_value=Path("/tmp")):
            with mock.patch("os.path.realpath", return_value="/real/path/here"):
                result = _pi_session_dir("/symlink/path")
                self.assertEqual(
                    result,
                    Path("/tmp/.pi/agent/sessions/--real-path-here--"),
                )


class PiSnapshotTests(unittest.TestCase):
    def test_snapshot_empty_when_dir_missing(self) -> None:
        result = _snapshot_session_paths(Path("/nonexistent/dir"))
        self.assertEqual(result, frozenset())

    def test_snapshot_captures_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.jsonl").write_text("{}")
            (root / "b.jsonl").write_text("{}")
            (root / "sub").mkdir()
            (root / "sub" / "c.jsonl").write_text("{}")
            snap = _snapshot_session_paths(root)
            self.assertEqual(len(snap), 3)


class PiSessionCaptureTests(unittest.TestCase):
    UUID = "019e26ac-8834-7582-93d5-e2aec599fe45"
    SESSION_NAME = f"2026-05-14T13-28-21-813Z_{UUID}.jsonl"

    def test_captures_session_created_by_invocation(self) -> None:
        with _PiHome("/tmp/my-instance") as home:
            # pre-existing session
            home.write_session("2026-05-14T10-old-uuid-11111111-2222-3333-4444-555555555555.jsonl")
            brain = PiBrain(Path("/tmp/my-instance"))
            brain._pre_state = brain.pre_invoke_snapshot()

            # new session created by this invocation
            home.write_session(self.SESSION_NAME)

            captured = brain.capture_session_id("2026-05-14T13:28:00Z")
            self.assertEqual(captured, self.UUID)

    def test_no_new_session_returns_none(self) -> None:
        with _PiHome("/tmp/my-instance") as home:
            home.write_session("2026-05-14T10-old-uuid-11111111-2222-3333-4444-555555555555.jsonl")
            brain = PiBrain(Path("/tmp/my-instance"))
            brain._pre_state = brain.pre_invoke_snapshot()
            # no new file
            captured = brain.capture_session_id("2026-05-14T13:28:00Z")
            self.assertIsNone(captured)

    def test_multiple_new_files_returns_none(self) -> None:
        with _PiHome("/tmp/my-instance") as home:
            home.write_session("2026-05-14T10-old-uuid-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl")
            brain = PiBrain(Path("/tmp/my-instance"))
            brain._pre_state = brain.pre_invoke_snapshot()

            # two new files appear (concurrent pi activity)
            home.write_session(f"2026-05-14T13-28_{'1'*8}-2222-3333-4444-555555555555.jsonl")
            home.write_session(f"2026-05-14T13-28_{'9'*8}-8888-7777-6666-555555555555.jsonl")

            captured = brain.capture_session_id("2026-05-14T13:28:00Z")
            self.assertIsNone(captured)

    def test_filename_not_matching_pattern_returns_none(self) -> None:
        with _PiHome("/tmp/my-instance") as home:
            home.write_session("old-session.jsonl")
            brain = PiBrain(Path("/tmp/my-instance"))
            brain._pre_state = brain.pre_invoke_snapshot()

            # new file but wrong format
            home.write_session("not-a-valid-session-name.jsonl")

            captured = brain.capture_session_id("2026-05-14T13:28:00Z")
            self.assertIsNone(captured)

    def test_empty_sessions_dir_is_safe(self) -> None:
        with _PiHome("/tmp/my-instance") as home:
            brain = PiBrain(Path("/tmp/my-instance"))
            brain._pre_state = brain.pre_invoke_snapshot()
            captured = brain.capture_session_id("2026-05-14T13:28:00Z")
            self.assertIsNone(captured)

    def test_pre_existing_sessions_are_ignored(self) -> None:
        with _PiHome("/tmp/my-instance") as home:
            for i in range(5):
                home.write_session(
                    f"2026-05-{i:02d}T10-{i:08d}-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
                )
            brain = PiBrain(Path("/tmp/my-instance"))
            brain._pre_state = brain.pre_invoke_snapshot()

            home.write_session(self.SESSION_NAME)

            captured = brain.capture_session_id("2026-05-14T13:28:00Z")
            self.assertEqual(captured, self.UUID)


class PiBrainExtraEnvTests(unittest.TestCase):
    def test_jc_pi_no_tools_defaults_to_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain = PiBrain(Path(tmp), override=BrainOverrideConfig())
            env = brain.extra_env()
            self.assertEqual(env["JC_PI_NO_TOOLS"], "1")

    def test_jc_pi_no_tools_respects_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain = PiBrain(Path(tmp), override=BrainOverrideConfig(no_tools=False))
            env = brain.extra_env()
            self.assertEqual(env["JC_PI_NO_TOOLS"], "0")

    def test_injects_api_keys_from_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text(
                "ANTHROPIC_API_KEY=sk-ant-test\n"
                "OPENAI_API_KEY=sk-openai-test\n",
                encoding="utf-8",
            )
            brain = PiBrain(instance, override=BrainOverrideConfig())
            env = brain.extra_env()
            self.assertEqual(env.get("ANTHROPIC_API_KEY"), "sk-ant-test")
            self.assertEqual(env.get("OPENAI_API_KEY"), "sk-openai-test")

    def test_injects_gemini_api_key_name_used_by_pi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text(
                "GEMINI_API_KEY=sk-gemini-test\n",
                encoding="utf-8",
            )
            brain = PiBrain(instance, override=BrainOverrideConfig())
            env = brain.extra_env()
            self.assertEqual(env.get("GEMINI_API_KEY"), "sk-gemini-test")

    def test_does_not_inject_missing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain = PiBrain(Path(tmp), override=BrainOverrideConfig())
            env = brain.extra_env()
            self.assertNotIn("ANTHROPIC_API_KEY", env)


class PiBrainExtraArgsTests(unittest.TestCase):
    def test_no_thinking_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain = PiBrain(Path(tmp), override=BrainOverrideConfig())
            args = brain.extra_args_for_event(_event())
            self.assertEqual(args, ())

    def test_thinking_passed_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain = PiBrain(Path(tmp), override=BrainOverrideConfig(thinking="high"))
            args = brain.extra_args_for_event(_event())
            self.assertEqual(args, ("--thinking", "high"))


class PiBrainPromptTests(unittest.TestCase):
    def test_prompt_contains_output_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brain = PiBrain(Path(tmp), override=BrainOverrideConfig())
            prompt = brain.prompt_for_event(_event(content="test message"))
            self.assertIn("[GATEWAY OUTPUT CONTRACT]", prompt)
            self.assertIn('"push_message_sent"', prompt)
            self.assertIn('"message"', prompt)
            self.assertIn("test message", prompt)


class PiShellAdapterTests(unittest.TestCase):
    """Verify the adapter script argv, stdout, and exit behavior.

    Tests adapter contract — NOT model behavior. Uses a fake pi binary
    that captures argv so we can assert the adapter builds the correct
    command line.
    """

    ADAPTER = REPO_ROOT / "lib" / "heartbeat" / "adapters" / "pi.sh"

    def _run_adapter(
        self,
        model: str = "",
        extra_args: list[str] | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> tuple[list[str], str, int]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            argv_file = root / "argv.txt"
            fake_pi = fake_bin / "pi"
            fake_pi.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$PI_ARGV_FILE\"\n"
                "cat >/dev/null\n"
                "printf 'fake ok\\n'\n",
                encoding="utf-8",
            )
            fake_pi.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            env["PI_ARGV_FILE"] = str(argv_file)
            if env_overrides:
                env.update(env_overrides)

            proc = subprocess.run(
                [str(self.ADAPTER), model, *(extra_args or [])],
                input="prompt from gateway",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            argv = (
                argv_file.read_text(encoding="utf-8").splitlines()
                if argv_file.exists()
                else []
            )
            return argv, proc.stdout, proc.returncode

    def test_default_argv_includes_no_context_and_no_extensions(self) -> None:
        argv, stdout, rc = self._run_adapter()
        self.assertIn("-p", argv)
        self.assertIn("--no-context-files", argv)
        self.assertIn("--no-extensions", argv)

    def test_default_argv_disables_prompt_discovery_surfaces(self) -> None:
        argv, stdout, rc = self._run_adapter()
        self.assertIn("--no-skills", argv)
        self.assertIn("--no-prompt-templates", argv)
        self.assertIn("--no-themes", argv)

    def test_default_argv_includes_no_tools(self) -> None:
        argv, stdout, rc = self._run_adapter()
        self.assertIn("--no-tools", argv)

    def test_jc_pi_no_tools_0_disables_no_tools_flag(self) -> None:
        argv, stdout, rc = self._run_adapter(
            env_overrides={"JC_PI_NO_TOOLS": "0"}
        )
        self.assertNotIn("--no-tools", argv)

    def test_model_sonnet_resolves_to_anthropic_claude_sonnet(self) -> None:
        argv, stdout, rc = self._run_adapter(model="sonnet")
        self.assertIn("--model", argv)
        idx = argv.index("--model")
        self.assertEqual(argv[idx + 1], "anthropic/claude-sonnet-4-6")

    def test_model_opus_resolves_to_anthropic_claude_opus(self) -> None:
        argv, stdout, rc = self._run_adapter(model="opus")
        idx = argv.index("--model")
        self.assertEqual(argv[idx + 1], "anthropic/claude-opus-4-7")

    def test_model_haiku_resolves_to_anthropic_claude_haiku(self) -> None:
        argv, stdout, rc = self._run_adapter(model="haiku")
        idx = argv.index("--model")
        self.assertEqual(argv[idx + 1], "anthropic/claude-haiku-4-5")

    def test_model_gpt5_resolves_to_openai(self) -> None:
        argv, stdout, rc = self._run_adapter(model="gpt-5.4")
        idx = argv.index("--model")
        self.assertEqual(argv[idx + 1], "openai/gpt-5.4")

    def test_empty_model_omits_model_flag(self) -> None:
        argv, stdout, rc = self._run_adapter(model="")
        self.assertNotIn("--model", argv)

    def test_provider_model_passed_through(self) -> None:
        argv, stdout, rc = self._run_adapter(model="openai/gpt-4o")
        idx = argv.index("--model")
        self.assertEqual(argv[idx + 1], "openai/gpt-4o")

    def test_resume_session_passed(self) -> None:
        argv, stdout, rc = self._run_adapter(
            env_overrides={"JC_RESUME_SESSION": "abc123-def456"}
        )
        self.assertIn("--session", argv)
        idx = argv.index("--session")
        self.assertEqual(argv[idx + 1], "abc123-def456")

    def test_worker_resume_session_fallback(self) -> None:
        argv, stdout, rc = self._run_adapter(
            env_overrides={"WORKER_RESUME_SESSION": "worker-session-id"}
        )
        self.assertIn("--session", argv)
        idx = argv.index("--session")
        self.assertEqual(argv[idx + 1], "worker-session-id")

    def test_extra_args_forwarded(self) -> None:
        argv, stdout, rc = self._run_adapter(extra_args=["--thinking", "high"])
        self.assertIn("--thinking", argv)
        self.assertIn("high", argv)

    def test_stdout_returned(self) -> None:
        argv, stdout, rc = self._run_adapter()
        self.assertIn("fake ok", stdout)

    def test_exit_zero_on_success(self) -> None:
        argv, stdout, rc = self._run_adapter()
        self.assertEqual(rc, 0)

    def test_pi_prefix_stripped_from_model(self) -> None:
        argv, stdout, rc = self._run_adapter(model="pi:sonnet")
        idx = argv.index("--model")
        self.assertEqual(argv[idx + 1], "anthropic/claude-sonnet-4-6")

    def test_adapter_is_executable(self) -> None:
        self.assertTrue(os.access(self.ADAPTER, os.X_OK))


class PiRegexTests(unittest.TestCase):
    def test_uuid_extraction_from_stem(self) -> None:
        """Path.stem strips .jsonl, so match against bare stem."""
        m = _PI_SESSION_UUID_RE.search(
            "2026-05-14T13-28-21-813Z_019e26ac-8834-7582-93d5-e2aec599fe45"
        )
        self.assertIsNotNone(m)
        if m:
            self.assertEqual(m.group(0), "019e26ac-8834-7582-93d5-e2aec599fe45")

    def test_uuid_extraction_from_full_filename_still_works(self) -> None:
        """search() finds the UUID anywhere in the string."""
        m = _PI_SESSION_UUID_RE.search(
            "2026-05-14T13-28-21-813Z_019e26ac-8834-7582-93d5-e2aec599fe45.jsonl"
        )
        self.assertIsNotNone(m)
        if m:
            self.assertEqual(m.group(0), "019e26ac-8834-7582-93d5-e2aec599fe45")

    def test_returns_none_for_no_uuid(self) -> None:
        self.assertIsNone(
            _PI_SESSION_UUID_RE.search("something-else")
        )

    def test_returns_none_for_timestamp_only(self) -> None:
        self.assertIsNone(
            _PI_SESSION_UUID_RE.search("2026-05-14T13-28-21-813Z")
        )


class PiBrainAdjustModelTests(unittest.TestCase):
    """PiBrain.adjust_model() upgrades to vision_model when session has image_url."""

    def _brain(self, vision_model: str | None = None, tmpdir: Path | None = None) -> PiBrain:
        override = BrainOverrideConfig(vision_model=vision_model)
        instance = tmpdir or Path(tempfile.mkdtemp())
        return PiBrain(instance, override=override)

    def test_returns_model_unchanged_when_no_vision_model_configured(self) -> None:
        brain = self._brain(vision_model=None)
        self.assertEqual(brain.adjust_model("deepseek-v4-flash", "some-uuid"), "deepseek-v4-flash")

    def test_returns_model_unchanged_when_no_resume_session(self) -> None:
        brain = self._brain(vision_model="deepseek-v4-pro")
        self.assertEqual(brain.adjust_model("deepseek-v4-flash", None), "deepseek-v4-flash")

    def test_returns_model_unchanged_when_already_vision_model(self) -> None:
        brain = self._brain(vision_model="deepseek-v4-pro")
        self.assertEqual(brain.adjust_model("deepseek-v4-pro", "some-uuid"), "deepseek-v4-pro")

    def test_upgrades_model_when_session_has_image_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            brain = self._brain(vision_model="deepseek-v4-pro", tmpdir=Path(tmpdir))
            # Pi stores images as {"type":"image","mimeType":"...","data":"..."} in
            # session JSONL (no spaces around colon — pi's own serializer).
            uuid = "019e3039-b427-7702-ab4c-d41d9b4f72ef"
            session_dir = _pi_session_dir(tmpdir)
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / f"2026-05-16T09-59-08-584Z_{uuid}.jsonl").write_bytes(
                b'{"type":"image","mimeType":"image/jpeg","data":"/9j/abc"}'
            )
            result = brain.adjust_model("deepseek-v4-flash", uuid)
            self.assertEqual(result, "deepseek-v4-pro")

    def test_keeps_model_when_session_has_no_image_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            brain = self._brain(vision_model="deepseek-v4-pro", tmpdir=Path(tmpdir))
            uuid = "019e3039-b427-7702-ab4c-d41d9b4f72ef"
            session_dir = _pi_session_dir(tmpdir)
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / f"2026-05-16T09-59-08-584Z_{uuid}.jsonl").write_bytes(
                b'{"role":"user","content":[{"type":"text","text":"hello"}]}'
            )
            result = brain.adjust_model("deepseek-v4-flash", uuid)
            self.assertEqual(result, "deepseek-v4-flash")

    def test_keeps_model_when_session_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            brain = self._brain(vision_model="deepseek-v4-pro", tmpdir=Path(tmpdir))
            result = brain.adjust_model("deepseek-v4-flash", "nonexistent-uuid")
            self.assertEqual(result, "deepseek-v4-flash")


class SessionHasImageUrlTests(unittest.TestCase):
    def test_detects_image_url(self) -> None:
        # Pi uses "type":"image" (no spaces) in session JSONL.
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            f.write(b'{"type":"image","mimeType":"image/jpeg","data":"abc"}')
            p = Path(f.name)
        self.assertTrue(_session_has_image_url(p))
        p.unlink()

    def test_returns_false_for_text_only(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            f.write(b'{"type":"text","text":"hello"}')
            p = Path(f.name)
        self.assertFalse(_session_has_image_url(p))
        p.unlink()

    def test_returns_false_for_missing_file(self) -> None:
        self.assertFalse(_session_has_image_url(Path("/nonexistent/path.jsonl")))


if __name__ == "__main__":
    unittest.main()
