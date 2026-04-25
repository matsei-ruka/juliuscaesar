"""Tests for the brain override parser."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import overrides  # noqa: E402


class InlineOverrideTests(unittest.TestCase):
    def test_short_alias_resolves(self):
        result = overrides.parse_inline_override("[opus] explain quantum tunneling")
        self.assertIsNotNone(result)
        self.assertEqual(result.spec, "claude:opus-4-7-1m")
        self.assertEqual(result.cleaned_content, "explain quantum tunneling")

    def test_full_spec_passed_through(self):
        result = overrides.parse_inline_override("[claude:sonnet-4-6] hi")
        self.assertEqual(result.spec, "claude:sonnet-4-6")
        self.assertEqual(result.cleaned_content, "hi")

    def test_codex_alias(self):
        result = overrides.parse_inline_override("[gpt5] write tests")
        self.assertEqual(result.spec, "codex:gpt-5")

    def test_no_prefix_returns_none(self):
        self.assertIsNone(overrides.parse_inline_override("just a message"))

    def test_empty_body_returns_none(self):
        self.assertIsNone(overrides.parse_inline_override("[opus]"))


class SlashCommandTests(unittest.TestCase):
    def test_brain_set(self):
        cmd = overrides.parse_slash_command("/brain opus")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.kind, "brain")
        self.assertEqual(cmd.spec, "claude:opus-4-7-1m")
        self.assertIn("sticky brain", cmd.reply.lower())

    def test_brain_help(self):
        cmd = overrides.parse_slash_command("/brain")
        self.assertEqual(cmd.kind, "help")
        self.assertIn("usage", cmd.reply.lower())

    def test_unrelated_slash(self):
        self.assertIsNone(overrides.parse_slash_command("/help"))

    def test_plain_text(self):
        self.assertIsNone(overrides.parse_slash_command("hello"))


if __name__ == "__main__":
    unittest.main()
