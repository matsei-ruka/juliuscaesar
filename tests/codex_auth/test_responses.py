"""Unit tests for `lib/codex_auth/responses.py`.

Replaces the network call (`_post`) with an in-memory queue so we can exercise
the 401-then-retry path and the SSE accumulation logic without HTTP.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_auth.client import CodexAuthClient
from codex_auth.responses import (
    DEFAULT_MODEL,
    RESPONSES_URL,
    ResponsesClient,
    ResponsesError,
    _accumulate_stream,
    _iter_sse_events,
    _extract_text,
)

from tests.codex_auth.test_client import FakeOpener, make_jwt, write_auth_file


SAMPLE_STREAM = (
    "event: response.created\n"
    "data: {\"type\":\"response.created\",\"response\":{\"id\":\"resp_1\"}}\n"
    "\n"
    "event: response.output_text.delta\n"
    "data: {\"type\":\"response.output_text.delta\",\"delta\":\"hello \"}\n"
    "\n"
    "event: response.output_text.delta\n"
    "data: {\"type\":\"response.output_text.delta\",\"delta\":\"world\"}\n"
    "\n"
    "event: response.completed\n"
    "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_1\","
    "\"model\":\"gpt-5.4-mini\",\"usage\":{\"input_tokens\":3,\"output_tokens\":2},"
    "\"output\":[{\"type\":\"message\",\"content\":[{\"type\":\"output_text\",\"text\":\"hello world\"}]}]}}\n"
    "\n"
).encode()


def _build_client(tmp_path: Path, *, opener=None):
    auth = tmp_path / "auth.json"
    write_auth_file(auth, access_exp=time.time() + 9 * 86400)
    return CodexAuthClient(auth_file=auth, opener=opener or FakeOpener([]))


class SseParserTests(unittest.TestCase):
    def test_iter_events(self):
        events = list(_iter_sse_events(SAMPLE_STREAM))
        types = [t for t, _ in events]
        self.assertEqual(
            types,
            [
                "response.created",
                "response.output_text.delta",
                "response.output_text.delta",
                "response.completed",
            ],
        )
        # Final event payload contains the response object.
        self.assertEqual(events[-1][1]["response"]["model"], "gpt-5.4-mini")

    def test_accumulate_stream_returns_concatenated_text(self):
        result = _accumulate_stream(SAMPLE_STREAM, "gpt-5.4-mini")
        self.assertEqual(result.text, "hello world")
        self.assertEqual(result.model, "gpt-5.4-mini")
        self.assertEqual(result.usage["input_tokens"], 3)

    def test_accumulate_stream_falls_back_to_done_text_when_no_deltas(self):
        stream = (
            "event: response.output_text.done\n"
            "data: {\"type\":\"response.output_text.done\",\"text\":\"final\"}\n"
            "\n"
            "event: response.completed\n"
            "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"r\","
            "\"model\":\"m\",\"output\":[]}}\n"
            "\n"
        ).encode()
        result = _accumulate_stream(stream, "m")
        self.assertEqual(result.text, "final")

    def test_stream_without_completed_raises(self):
        stream = (
            "event: response.output_text.delta\n"
            "data: {\"delta\":\"partial\"}\n"
            "\n"
        ).encode()
        with self.assertRaises(ResponsesError) as cm:
            _accumulate_stream(stream, "m")
        self.assertEqual(cm.exception.status, 502)

    def test_stream_failure_event_raises(self):
        stream = (
            "event: response.failed\n"
            "data: {\"error\":{\"message\":\"upstream went bang\"}}\n"
            "\n"
        ).encode()
        with self.assertRaises(ResponsesError) as cm:
            _accumulate_stream(stream, "m")
        self.assertEqual(cm.exception.status, 500)
        self.assertIn("upstream went bang", str(cm.exception))


class ExtractTextTests(unittest.TestCase):
    def test_top_level_output_text(self):
        self.assertEqual(_extract_text({"output_text": "hi"}), "hi")

    def test_responses_api_output_array(self):
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "Hi"},
                        {"type": "output_text", "text": " there"},
                    ],
                }
            ]
        }
        self.assertEqual(_extract_text(payload), "Hi there")


class ResponsesClientTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def test_default_url_is_chatgpt_backend(self):
        client = ResponsesClient(_build_client(self.tmp_path), default_model=DEFAULT_MODEL)
        self.assertEqual(client.url, RESPONSES_URL)
        self.assertIn("chatgpt.com", client.url)

    def test_complete_success_request_shape(self):
        auth = _build_client(self.tmp_path)
        sent = []

        def fake_post(token, account_id, body):
            sent.append((token, account_id, body))
            return 200, SAMPLE_STREAM

        client = ResponsesClient(auth, default_model="gpt-5.4-mini")
        with mock.patch.object(client, "_post", side_effect=fake_post):
            result = client.complete("ping", instructions="be terse", max_output_tokens=10)
        self.assertEqual(result.text, "hello world")
        self.assertEqual(len(sent), 1)
        token, account_id, body = sent[0]
        self.assertTrue(token)
        self.assertEqual(account_id, "acc-uuid-1")  # from JWT payload
        self.assertEqual(body["model"], "gpt-5.4-mini")
        # ChatGPT backend requires list-shaped input + store=false + stream=true.
        self.assertEqual(body["input"], [{"role": "user", "content": "ping"}])
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertEqual(body["instructions"], "be terse")
        # max_output_tokens is silently dropped — endpoint rejects it.
        self.assertNotIn("max_output_tokens", body)

    def test_complete_default_instructions_when_none(self):
        auth = _build_client(self.tmp_path)
        sent = []

        def fake_post(token, account_id, body):
            sent.append(body)
            return 200, SAMPLE_STREAM

        client = ResponsesClient(auth)
        with mock.patch.object(client, "_post", side_effect=fake_post):
            client.complete("ping")
        self.assertTrue(sent[0]["instructions"])  # endpoint requires non-empty

    def test_complete_401_then_force_refresh_then_success(self):
        # First _post returns 401; client must force-refresh and retry.
        auth = _build_client(
            self.tmp_path,
            opener=FakeOpener(
                [
                    (
                        200,
                        json.dumps(
                            {
                                "access_token": make_jwt(exp=time.time() + 9 * 86400),
                                "id_token": "id.body.sig",
                                "refresh_token": "rt_new",
                                "expires_in": 600000,
                            }
                        ).encode(),
                    )
                ]
            ),
        )
        client = ResponsesClient(auth, default_model="gpt-5.4-mini")
        responses_iter = iter(
            [
                (401, b'{"detail":"unauthorized"}'),
                (200, SAMPLE_STREAM),
            ]
        )

        def fake_post(token, account_id, body):
            return next(responses_iter)

        with mock.patch.object(client, "_post", side_effect=fake_post):
            result = client.complete("ping")
        self.assertEqual(result.text, "hello world")

    def test_complete_persistent_401_raises(self):
        auth = _build_client(
            self.tmp_path,
            opener=FakeOpener(
                [
                    (
                        200,
                        json.dumps(
                            {
                                "access_token": make_jwt(exp=time.time() + 9 * 86400),
                                "id_token": "id.body.sig",
                            }
                        ).encode(),
                    )
                ]
            ),
        )
        client = ResponsesClient(auth, default_model="gpt-5.4-mini")
        with mock.patch.object(client, "_post", return_value=(401, b'{"detail":"nope"}')):
            with self.assertRaises(ResponsesError) as cm:
                client.complete("ping")
        self.assertEqual(cm.exception.status, 401)

    def test_complete_5xx_raises_responses_error(self):
        auth = _build_client(self.tmp_path)
        client = ResponsesClient(auth, default_model="gpt-5.4-mini")
        with mock.patch.object(client, "_post", return_value=(500, b'{"detail":"upstream"}')):
            with self.assertRaises(ResponsesError) as cm:
                client.complete("ping")
        self.assertEqual(cm.exception.status, 500)
        self.assertIn("upstream", str(cm.exception))

    def test_complete_400_with_codex_detail_message(self):
        auth = _build_client(self.tmp_path)
        client = ResponsesClient(auth, default_model="gpt-5.4-mini")
        with mock.patch.object(
            client,
            "_post",
            return_value=(400, b'{"detail":"Stream must be set to true"}'),
        ):
            with self.assertRaises(ResponsesError) as cm:
                client.complete("ping")
        self.assertIn("Stream must be set to true", str(cm.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
