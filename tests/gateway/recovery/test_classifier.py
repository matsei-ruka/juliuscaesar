"""Recovery classifier — regex prefilter + LLM mocking."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.config import GatewayConfig, TriageConfig  # noqa: E402
from gateway.queue import Event  # noqa: E402
from gateway.recovery import classifier  # noqa: E402


def _event(content: str = "hello") -> Event:
    return Event(
        id=1,
        source="telegram",
        source_message_id="m1",
        user_id="u1",
        conversation_id="c1",
        content=content,
        meta=None,
        status="running",
        received_at="2026-04-26T00:00:00Z",
        available_at="2026-04-26T00:00:00Z",
        locked_by="w1",
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )


class RegexPrefilterTests(unittest.TestCase):
    def test_econnreset_classifies_transient(self):
        c = classifier.regex_prefilter("ECONNRESET reading from upstream")
        self.assertIsNotNone(c)
        self.assertEqual(c.kind, "transient")
        self.assertEqual(c.source, "regex")

    def test_session_missing_extracts_uuid(self):
        stderr = "Error: No conversation found with session ID 7d5ec0b5-47a6-4ff3-ae5f-2a6a6657cf46"
        c = classifier.regex_prefilter(stderr)
        self.assertIsNotNone(c)
        self.assertEqual(c.kind, "session_missing")
        self.assertEqual(c.extracted["session_id"], "7d5ec0b5-47a6-4ff3-ae5f-2a6a6657cf46")

    def test_session_expired_extracts_login_url(self):
        stderr = (
            "Your session has expired. Please run: claude /login\n"
            "Visit https://claude.ai/cli/auth?token=abc123 to re-authenticate."
        )
        c = classifier.regex_prefilter(stderr)
        self.assertIsNotNone(c)
        self.assertEqual(c.kind, "session_expired")
        self.assertIn("claude.ai", c.extracted.get("login_url", ""))

    def test_login_url_must_be_allowlisted(self):
        stderr = "please run /login\nvisit https://evil.example.com/login here"
        c = classifier.regex_prefilter(stderr)
        self.assertIsNotNone(c)
        self.assertEqual(c.kind, "session_expired")
        # Foreign hosts must not be extracted.
        self.assertNotIn("login_url", c.extracted)

    def test_bad_input_image_oversize(self):
        c = classifier.regex_prefilter("Image exceeds maximum size of 5MB (got 12MB)")
        self.assertIsNotNone(c)
        self.assertEqual(c.kind, "bad_input")

    def test_unknown_returns_none(self):
        c = classifier.regex_prefilter("something weird and unmatched happened here")
        self.assertIsNone(c)

    def test_empty_stderr_returns_none(self):
        self.assertIsNone(classifier.regex_prefilter(""))


class LLMClassifyTests(unittest.TestCase):
    def _config(self) -> GatewayConfig:
        return GatewayConfig(
            triage=TriageConfig(
                backend="openrouter",
                openrouter_api_key_env="FAKE_KEY",
                openrouter_model="meta-llama/llama-3.1-8b-instruct",
            ),
        )

    def test_falls_back_to_unknown_on_classifier_outage(self):
        with mock.patch.object(classifier, "regex_prefilter", return_value=None), \
             mock.patch.object(classifier, "llm_classify", return_value=None), \
             mock.patch("pathlib.Path.exists", return_value=True):
            c = classifier.classify(
                _event("hi"),
                "totally unrecognized error",
                config=self._config(),
                instance_dir=Path("/nonexistent"),
            )
        self.assertEqual(c.kind, "unknown")
        self.assertEqual(c.source, "fallback")

    def test_llm_low_confidence_demoted_to_unknown(self):
        with mock.patch.object(classifier, "regex_prefilter", return_value=None), \
             mock.patch.object(
                 classifier,
                 "llm_classify",
                 return_value=classifier.Classification(
                     kind="bad_input",
                     confidence=0.3,
                     extracted={},
                     raw="{}",
                     source="llm",
                 ),
             ):
            c = classifier.classify(
                _event("hi"),
                "weird error",
                config=self._config(),
                instance_dir=Path("/tmp"),
            )
        self.assertEqual(c.kind, "unknown")
        self.assertEqual(c.confidence, 0.3)


if __name__ == "__main__":
    unittest.main()
