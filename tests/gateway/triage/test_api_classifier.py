"""Tests for protocol-aware HTTP triage classifiers."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.config import ConfigError, TriageConfig, load_config  # noqa: E402
from gateway.triage import ApiClassifierTriage  # noqa: E402
from gateway.triage.factory import build_backend  # noqa: E402


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _instance() -> Path:
    return Path(tempfile.mkdtemp())


class ApiClassifierTests(unittest.TestCase):
    def test_openai_compat_success(self):
        instance = _instance()
        captured = {}
        cfg = TriageConfig(
            backend="api_classifier",
            protocol="openai_compat",
            base_url="https://api.deepseek.com/v1",
            api_key_env="DEEPSEEK_API_KEY",
            model="deepseek-chat",
            timeout_seconds=7,
            max_tokens=200,
        )

        def fake_urlopen(req, *, timeout):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"class":"analysis","confidence":0.92}'
                            }
                        }
                    ]
                }
            )

        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False), \
             mock.patch("gateway.triage.api_classifier.urllib.request.urlopen", side_effect=fake_urlopen):
            result = ApiClassifierTriage(cfg, instance).classify("compare providers")

        self.assertEqual(result.class_, "analysis")
        self.assertAlmostEqual(result.confidence, 0.92)
        self.assertEqual(captured["url"], "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(captured["body"]["model"], "deepseek-chat")
        self.assertEqual(captured["body"]["max_tokens"], 200)
        self.assertEqual(captured["timeout"], 7)

    def test_anthropic_success(self):
        instance = _instance()
        captured = {}
        cfg = TriageConfig(
            backend="api_classifier",
            protocol="anthropic",
            base_url="https://api.anthropic.com/v1",
            api_key_env="ANTHROPIC_API_KEY",
            model="claude-haiku-4-5",
            max_tokens=128,
        )

        def fake_urlopen(req, *, timeout):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse(
                {
                    "content": [
                        {"type": "text", "text": '{"class":"quick","confidence":0.81}'}
                    ],
                    "stop_reason": "end_turn",
                }
            )

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "anthropic-key"}, clear=False), \
             mock.patch("gateway.triage.api_classifier.urllib.request.urlopen", side_effect=fake_urlopen):
            result = ApiClassifierTriage(cfg, instance).classify("short question")

        self.assertEqual(result.class_, "quick")
        self.assertAlmostEqual(result.confidence, 0.81)
        self.assertEqual(captured["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(captured["headers"]["X-api-key"], "anthropic-key")
        self.assertEqual(captured["headers"]["Anthropic-version"], "2023-06-01")
        self.assertEqual(captured["body"]["max_tokens"], 128)
        self.assertEqual(captured["body"]["system"], "Output exactly one JSON object on one line.")

    def test_missing_key_returns_failure_result(self):
        instance = _instance()
        cfg = TriageConfig(
            backend="api_classifier",
            protocol="openai_compat",
            base_url="https://api.deepseek.com/v1",
            api_key_env="MISSING_TRIAGE_KEY",
            model="deepseek-chat",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            result = ApiClassifierTriage(cfg, instance).classify("hello")

        self.assertEqual(result.class_, "quick")
        self.assertEqual(result.confidence, 0.0)
        self.assertIn("missing MISSING_TRIAGE_KEY", result.raw or "")

    def test_http_and_network_failures_are_graceful(self):
        instance = _instance()
        cfg = TriageConfig(
            backend="api_classifier",
            protocol="openai_compat",
            base_url="https://api.deepseek.com/v1",
            api_key_env="DEEPSEEK_API_KEY",
            model="deepseek-chat",
        )
        failures = [
            HTTPError("https://example.test", 401, "Unauthorized", hdrs=None, fp=None),
            URLError("down"),
            TimeoutError("slow"),
        ]
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False):
            for failure in failures:
                with mock.patch(
                    "gateway.triage.api_classifier.urllib.request.urlopen",
                    side_effect=failure,
                ):
                    result = ApiClassifierTriage(cfg, instance).classify("hello")
                self.assertEqual(result.class_, "quick")
                self.assertEqual(result.confidence, 0.0)

    def test_anthropic_truncation_failure_preserves_raw(self):
        instance = _instance()
        cfg = TriageConfig(
            backend="api_classifier",
            protocol="anthropic",
            base_url="https://api.anthropic.com/v1",
            api_key_env="ANTHROPIC_API_KEY",
            model="claude-haiku-4-5",
            max_tokens=5,
        )

        def fake_urlopen(req, *, timeout):
            return FakeResponse(
                {
                    "content": [{"type": "text", "text": '{"class":"analysis"'}],
                    "stop_reason": "max_tokens",
                }
            )

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "anthropic-key"}, clear=False), \
             mock.patch("gateway.triage.api_classifier.urllib.request.urlopen", side_effect=fake_urlopen):
            result = ApiClassifierTriage(cfg, instance).classify("hello")

        self.assertEqual(result.class_, "quick")
        self.assertEqual(result.confidence, 0.0)
        self.assertIn("anthropic truncated triage output", result.raw or "")

    def test_openrouter_shim_preserves_request_shape(self):
        instance = _instance()
        captured = {}
        cfg = TriageConfig(
            backend="openrouter",
            openrouter_api_key_env="OPENROUTER_API_KEY",
            openrouter_model="meta-llama/llama-3.1-8b-instruct",
            openrouter_timeout_seconds=5,
        )

        def fake_urlopen(req, *, timeout):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse(
                {
                    "choices": [
                        {"message": {"content": '{"class":"smalltalk","confidence":0.9}'}}
                    ]
                }
            )

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "or-key"}, clear=False), \
             mock.patch("gateway.triage.api_classifier.urllib.request.urlopen", side_effect=fake_urlopen):
            result = build_backend(cfg, instance).classify("hi")

        self.assertEqual(result.class_, "smalltalk")
        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer or-key")
        self.assertEqual(captured["headers"]["Http-referer"], "https://github.com/matsei-ruka/juliuscaesar")
        self.assertEqual(captured["headers"]["X-title"], "JuliusCaesar Gateway")
        self.assertEqual(captured["body"]["model"], "meta-llama/llama-3.1-8b-instruct")
        self.assertEqual(captured["body"]["temperature"], 0.0)
        self.assertEqual(captured["timeout"], 5)


class ApiClassifierConfigTests(unittest.TestCase):
    def _load(self, text: str):
        instance = _instance()
        (instance / "ops").mkdir()
        (instance / "ops" / "gateway.yaml").write_text(text, encoding="utf-8")
        return load_config(instance)

    def test_config_loader_accepts_top_level_api_classifier_fields(self):
        cfg = self._load(
            "triage: api_classifier\n"
            "triage_protocol: openai_compat\n"
            "triage_base_url: https://api.deepseek.com/v1\n"
            "triage_api_key_env: DEEPSEEK_API_KEY\n"
            "triage_model: deepseek-chat\n"
            "triage_timeout_seconds: 6\n"
            "triage_max_tokens: 200\n"
        )

        self.assertEqual(cfg.triage.backend, "api_classifier")
        self.assertEqual(cfg.triage.protocol, "openai_compat")
        self.assertEqual(cfg.triage.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(cfg.triage.api_key_env, "DEEPSEEK_API_KEY")
        self.assertEqual(cfg.triage.model, "deepseek-chat")
        self.assertEqual(cfg.triage.timeout_seconds, 6)
        self.assertEqual(cfg.triage.max_tokens, 200)

    def test_config_loader_accepts_nested_api_classifier_fields(self):
        cfg = self._load(
            "triage:\n"
            "  backend: api_classifier\n"
            "  protocol: anthropic\n"
            "  base_url: https://api.anthropic.com/v1\n"
            "  api_key_env: ANTHROPIC_API_KEY\n"
            "  model: claude-haiku-4-5\n"
            "  timeout_seconds: 6\n"
            "  max_tokens: 200\n"
        )

        self.assertEqual(cfg.triage.backend, "api_classifier")
        self.assertEqual(cfg.triage.protocol, "anthropic")
        self.assertEqual(cfg.triage.max_tokens, 200)

    def test_config_validation_rejects_bad_combos(self):
        cases = [
            (
                "triage: api_classifier\n"
                "triage_api_key_env: KEY\n"
                "triage_model: model\n",
                "triage_base_url",
            ),
            (
                "triage: api_classifier\n"
                "triage_protocol: bogus\n"
                "triage_base_url: https://api.example.test/v1\n"
                "triage_api_key_env: KEY\n"
                "triage_model: model\n",
                "unsupported protocol",
            ),
            (
                "triage: openrouter\n"
                "triage_protocol: openai_compat\n",
                "only valid when triage backend is api_classifier",
            ),
            (
                "triage: api_classifier\n"
                "triage_protocol: anthropic\n"
                "triage_base_url: https://api.anthropic.com/v1\n"
                "triage_api_key_env: KEY\n"
                "triage_model: model\n",
                "triage_max_tokens",
            ),
            (
                "triage: api_classifier\n"
                "triage_base_url: ftp://example.test\n"
                "triage_api_key_env: KEY\n"
                "triage_model: model\n",
                "must start with http:// or https://",
            ),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(ConfigError) as ctx:
                    self._load(text)
                self.assertIn(expected, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
