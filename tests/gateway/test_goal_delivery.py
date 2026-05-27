"""End-to-end test of goal delivery through Brain.invoke + adapters.

A fake adapter captures its stdin (the prompt) and the JC_GOAL env, so we can
assert the two delivery policies without a real CLI:
  - body class: <goal> block prepended to the prompt, first turn only.
  - system-prompt class: JC_GOAL env set (adapter would --append-system-prompt).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import goal_cache  # noqa: E402
from gateway.brains.base import Brain  # noqa: E402
from gateway.brains.claude import ClaudeBrain  # noqa: E402
from gateway.brains.pi import PiBrain  # noqa: E402
from gateway.brains.codex import CodexBrain  # noqa: E402
from gateway.config import BrainOverrideConfig  # noqa: E402
from gateway.queue import Event  # noqa: E402


def _event(conversation_id: str) -> Event:
    return Event(
        id=1,
        source="company-inbox",
        source_message_id="task:t1",
        user_id=None,
        conversation_id=conversation_id,
        content="user message body",
        meta=None,
        status="running",
        received_at="2026-05-26T00:00:00Z",
        available_at="2026-05-26T00:00:00Z",
        locked_by="w",
        locked_until=None,
        started_at="2026-05-26T00:00:00Z",
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


class _BodyBrain(Brain):
    name = "fakebody"
    needs_l1_preamble = True
    # goal_delivery defaults to "body"


class _SysBrain(Brain):
    name = "fakesys"
    needs_l1_preamble = True
    goal_delivery = "system_prompt"


class GoalDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inst = Path(self.tmp.name)
        self.prompt_out = self.inst / "prompt.txt"
        self.goal_out = self.inst / "goal.txt"
        adapter = self.inst / "fake.sh"
        adapter.write_text(
            "#!/usr/bin/env bash\n"
            f'cat > "{self.prompt_out}"\n'
            f'printf "%s" "${{JC_GOAL:-}}" > "{self.goal_out}"\n'
            'printf \'{"push_message_sent": false, "message": "ok"}\'\n'
        )
        adapter.chmod(0o755)
        self.adapter = adapter

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, brain_cls, *, resume_session):
        brain = brain_cls(self.inst, override=BrainOverrideConfig(bin=str(self.adapter)))
        brain.invoke(
            event=_event("task-root:R"),
            model=None,
            resume_session=resume_session,
            timeout_seconds=30,
            log_path=self.inst / "adapter.log",
        )
        prompt = self.prompt_out.read_text() if self.prompt_out.exists() else ""
        goal_env = self.goal_out.read_text() if self.goal_out.exists() else ""
        return prompt, goal_env

    def test_body_class_prepends_goal_on_fresh_session(self):
        goal_cache.set(self.inst, "task-root:R", "t1", "Ship the thing")
        prompt, goal_env = self._run(_BodyBrain, resume_session=None)
        self.assertIn("<goal>", prompt)
        self.assertIn("Ship the thing", prompt)
        self.assertEqual(goal_env, "")  # body class does not use JC_GOAL

    def test_body_class_skips_goal_on_resume(self):
        goal_cache.set(self.inst, "task-root:R", "t1", "Ship the thing")
        prompt, _ = self._run(_BodyBrain, resume_session="sess-1")
        self.assertNotIn("<goal>", prompt)

    def test_system_prompt_class_sets_env_not_body(self):
        goal_cache.set(self.inst, "task-root:R", "t1", "Ship the thing")
        prompt, goal_env = self._run(_SysBrain, resume_session=None)
        self.assertEqual(goal_env, "Ship the thing")
        self.assertNotIn("<goal>", prompt)

    def test_no_goal_no_injection(self):
        prompt, goal_env = self._run(_BodyBrain, resume_session=None)
        self.assertNotIn("<goal>", prompt)
        self.assertEqual(goal_env, "")

    def test_system_prompt_class_env_every_turn_even_on_resume(self):
        # System-prompt anchor is re-applied each call (ephemeral), incl. resume.
        goal_cache.set(self.inst, "task-root:R", "t1", "Ship the thing")
        _, goal_env = self._run(_SysBrain, resume_session="sess-1")
        self.assertEqual(goal_env, "Ship the thing")


class BrainClassTests(unittest.TestCase):
    def test_class_attrs(self):
        inst = Path("/tmp/x")
        self.assertEqual(ClaudeBrain(inst).goal_delivery, "system_prompt")
        self.assertEqual(PiBrain(inst).goal_delivery, "system_prompt")
        self.assertEqual(CodexBrain(inst).goal_delivery, "body")  # default


class AdapterScriptTests(unittest.TestCase):
    """claude.sh / pi.sh turn JC_GOAL into --append-system-prompt."""

    def _run_adapter(self, adapter_name: str, *, with_goal: bool):
        adapters = REPO_ROOT / "lib" / "heartbeat" / "adapters"
        with tempfile.TemporaryDirectory() as tmp:
            # The adapters prepend "$HOME/.local/bin" to PATH, so place the fake
            # CLI there (sandboxed HOME) to win over any real install.
            home = Path(tmp)
            bindir = home / ".local" / "bin"
            bindir.mkdir(parents=True)
            fake = bindir / adapter_name.replace(".sh", "")
            fake.write_text('#!/usr/bin/env bash\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
            fake.chmod(0o755)
            env = dict(os.environ)
            env["HOME"] = str(home)
            if with_goal:
                env["JC_GOAL"] = "ANCHOR-GOAL-TEXT"
            else:
                env.pop("JC_GOAL", None)
            import subprocess

            proc = subprocess.run(
                ["bash", str(adapters / adapter_name), ""],
                input="hello\n",
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            return proc.stdout

    def test_claude_sh_appends_goal_when_set(self):
        out = self._run_adapter("claude.sh", with_goal=True)
        self.assertIn("--append-system-prompt", out)
        self.assertIn("ANCHOR-GOAL-TEXT", out)

    def test_claude_sh_no_goal_arg_when_unset(self):
        out = self._run_adapter("claude.sh", with_goal=False)
        self.assertNotIn("ANCHOR-GOAL-TEXT", out)

    def test_pi_sh_appends_goal_when_set(self):
        out = self._run_adapter("pi.sh", with_goal=True)
        self.assertIn("ANCHOR-GOAL-TEXT", out)


if __name__ == "__main__":
    unittest.main()
