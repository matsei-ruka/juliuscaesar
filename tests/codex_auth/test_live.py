"""Integration smoke tests against the operator's live Codex auth state.

Skipped by default. Set ``CODEX_AUTH_LIVE=1`` in the environment to enable.

These tests touch the network (auth refresh + Responses API) and the local
``~/.codex/auth.json`` file (read-only, but they will trigger a refresh if
the token is within the skew window).
"""

from __future__ import annotations

import os
import unittest

from codex_auth import CodexAuthClient, ResponsesClient
from codex_auth.responses import DEFAULT_MODEL


@unittest.skipUnless(
    os.environ.get("CODEX_AUTH_LIVE") == "1",
    "set CODEX_AUTH_LIVE=1 to run live integration smoke",
)
class LiveSmokeTests(unittest.TestCase):
    def test_status_reports_chatgpt_account(self):
        snap = CodexAuthClient().status()
        self.assertEqual(snap["auth_mode"], "chatgpt")
        self.assertGreater(snap["expires_in_seconds"], 0)
        self.assertIsNotNone(snap["account_id"])

    def test_responses_api_returns_text(self):
        client = ResponsesClient(
            CodexAuthClient(), default_model=DEFAULT_MODEL, timeout_seconds=60
        )
        result = client.complete(
            "Reply with exactly one word: pong",
            instructions="You are terse.",
        )
        self.assertTrue(result.text.strip())
        self.assertIn("pong", result.text.lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
