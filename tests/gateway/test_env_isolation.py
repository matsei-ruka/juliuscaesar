"""Tests for ``lib.gateway.env_isolation`` — see
``docs/specs/gateway-env-isolation.md``."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.env_isolation import (  # noqa: E402
    DANGEROUS_KEYS,
    WHITELIST_KEYS,
    is_dangerous,
    is_whitelisted,
    sanitize,
)


class IsDangerousTests(unittest.TestCase):
    def test_explicit_keys(self):
        for k in DANGEROUS_KEYS:
            self.assertTrue(is_dangerous(k), msg=k)

    def test_codex_prefix(self):
        self.assertTrue(is_dangerous("CODEX_HOME"))
        self.assertTrue(is_dangerous("CODEX_AUTH"))

    def test_claude_prefix(self):
        self.assertTrue(is_dangerous("CLAUDE_CONFIG_DIR"))
        self.assertTrue(is_dangerous("CLAUDE_ARGS_EXTRA"))

    def test_benign_keys_not_flagged(self):
        self.assertFalse(is_dangerous("HOME"))
        self.assertFalse(is_dangerous("PATH"))
        self.assertFalse(is_dangerous("FOO"))


class IsWhitelistedTests(unittest.TestCase):
    def test_runtime_basics(self):
        for k in ("HOME", "USER", "PATH", "SHELL", "LANG", "TZ", "PWD"):
            self.assertTrue(is_whitelisted(k), msg=k)

    def test_lc_prefix(self):
        self.assertTrue(is_whitelisted("LC_ALL"))
        self.assertTrue(is_whitelisted("LC_CTYPE"))

    def test_unknown_keys(self):
        self.assertFalse(is_whitelisted("TELEGRAM_BOT_TOKEN"))
        self.assertFalse(is_whitelisted("FOO"))


class SanitizeTests(unittest.TestCase):
    def test_strips_dangerous_parent_token_when_dotenv_silent(self):
        parent = {
            "HOME": "/home/jc",
            "PATH": "/usr/bin",
            "TELEGRAM_BOT_TOKEN": "POISON",
        }
        clean, stripped = sanitize(parent, {})
        self.assertNotIn("TELEGRAM_BOT_TOKEN", clean)
        self.assertIn("TELEGRAM_BOT_TOKEN", stripped)
        # Whitelist survives.
        self.assertEqual(clean["HOME"], "/home/jc")
        self.assertEqual(clean["PATH"], "/usr/bin")

    def test_dotenv_overrides_parent(self):
        parent = {
            "HOME": "/home/jc",
            "TELEGRAM_BOT_TOKEN": "POISON",
        }
        dotenv = {"TELEGRAM_BOT_TOKEN": "CORRECT"}
        clean, stripped = sanitize(parent, dotenv)
        self.assertEqual(clean["TELEGRAM_BOT_TOKEN"], "CORRECT")
        # When .env supplies the key, we don't list it as stripped —
        # it's not a leak, it's an override.
        self.assertNotIn("TELEGRAM_BOT_TOKEN", stripped)

    def test_non_whitelisted_non_dangerous_parent_keys_dropped(self):
        parent = {"HOME": "/h", "FOOBAR": "junk"}
        clean, _ = sanitize(parent, {})
        self.assertNotIn("FOOBAR", clean)
        self.assertIn("HOME", clean)

    def test_lc_keys_preserved(self):
        parent = {"LC_ALL": "en_US.UTF-8", "LC_CTYPE": "UTF-8", "HOME": "/h"}
        clean, _ = sanitize(parent, {})
        self.assertEqual(clean["LC_ALL"], "en_US.UTF-8")
        self.assertEqual(clean["LC_CTYPE"], "UTF-8")

    def test_dotenv_arbitrary_keys_pass_through(self):
        parent = {"HOME": "/h"}
        dotenv = {"BNESIM_TENANT": "t1", "VOICE_PROVIDER": "dashscope"}
        clean, _ = sanitize(parent, dotenv)
        self.assertEqual(clean["BNESIM_TENANT"], "t1")
        self.assertEqual(clean["VOICE_PROVIDER"], "dashscope")

    def test_multiple_dangerous_keys_listed_sorted(self):
        parent = {
            "HOME": "/h",
            "OPENAI_API_KEY": "x",
            "ANTHROPIC_API_KEY": "y",
            "CODEX_HOME": "z",
        }
        _, stripped = sanitize(parent, {})
        self.assertEqual(
            stripped,
            ["ANTHROPIC_API_KEY", "CODEX_HOME", "OPENAI_API_KEY"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
