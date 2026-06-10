"""Audit feature 6 — Telegram outbound hardening remainder.

4096 chunked sends, 429 retry_after on sends, offset advance surviving
enqueue failure, send_photo response check.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import config as gateway_config  # noqa: E402
from gateway.channels import telegram as telegram_module  # noqa: E402
from gateway.channels import telegram_outbound  # noqa: E402
from gateway.channels.telegram import TelegramChannel  # noqa: E402
from gateway.channels.telegram_outbound import (  # noqa: E402
    send_text,
    split_for_telegram,
)
from gateway.config import ChannelConfig  # noqa: E402
from gateway.format import to_markdown_v2  # noqa: E402


class SplitForTelegramTests(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(split_for_telegram("hello world"), ["hello world"])

    def test_chunks_fit_after_escaping(self):
        text = "\n\n".join(f"paragraph {i} " + "word. " * 80 for i in range(30))
        chunks = split_for_telegram(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(to_markdown_v2(chunk)), 4096)
        # No content lost (whitespace joins aside).
        joined = "".join(c.replace("\n", "").replace(" ", "") for c in chunks)
        original = text.replace("\n", "").replace(" ", "")
        self.assertEqual(joined, original)

    def test_fences_never_left_open(self):
        code = "\n".join(f"line_{i} = compute_{i}()  # comment {i}" for i in range(400))
        text = f"intro paragraph\n\n```python\n{code}\n```\n\noutro paragraph"
        chunks = split_for_telegram(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            fence_lines = [
                line for line in chunk.split("\n") if line.lstrip().startswith("```")
            ]
            self.assertEqual(
                len(fence_lines) % 2, 0,
                f"chunk has unbalanced fences:\n{chunk[:200]}",
            )
            self.assertLessEqual(len(to_markdown_v2(chunk)), 4096)

    def test_single_oversize_line_hard_split(self):
        text = "x" * 12000
        chunks = split_for_telegram(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        for chunk in chunks:
            self.assertLessEqual(len(to_markdown_v2(chunk)), 4096)


class SendTextChunkingTests(unittest.TestCase):
    def _run_send(self, response, posts):
        def fake_http_json(url, *, data=None, timeout=15, **kw):
            posts.append(data)
            return {"ok": True, "result": {"message_id": len(posts)}}

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(telegram_outbound, "http_json", fake_http_json):
                return send_text(
                    instance_dir=Path(tmp),
                    token="tok",
                    response=response,
                    meta={"chat_id": "1", "message_id": 42},
                    log=lambda _m: None,
                )

    def test_long_reply_sent_in_order_reply_to_first_only(self):
        posts: list = []
        text = "\n\n".join(f"para {i} " + "x " * 400 for i in range(8))
        last_id = self._run_send(text, posts)
        self.assertGreater(len(posts), 1)
        self.assertEqual(posts[0].get("reply_to_message_id"), 42)
        for payload in posts[1:]:
            self.assertNotIn("reply_to_message_id", payload)
        self.assertEqual(last_id, str(len(posts)))  # last chunk's message_id

    def test_short_reply_single_post(self):
        posts: list = []
        self._run_send("hi", posts)
        self.assertEqual(len(posts), 1)


class SendRetry429Tests(unittest.TestCase):
    def test_429_honors_retry_after_then_succeeds(self):
        calls = {"n": 0}
        sleeps: list[float] = []

        def fake_http_json(url, *, data=None, timeout=15, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "ok": False,
                    "error_code": 429,
                    "parameters": {"retry_after": 2},
                }
            return {"ok": True, "result": {"message_id": 7}}

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(telegram_outbound, "http_json", fake_http_json), \
                 mock.patch.object(telegram_outbound.time, "sleep", sleeps.append):
                message_id = send_text(
                    instance_dir=Path(tmp),
                    token="tok",
                    response="hello",
                    meta={"chat_id": "1"},
                    log=lambda _m: None,
                )
        self.assertEqual(message_id, "7")
        self.assertEqual(sleeps, [2.0])

    def test_429_exhausted_raises(self):
        def fake_http_json(url, *, data=None, timeout=15, **kw):
            return {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(telegram_outbound, "http_json", fake_http_json), \
                 mock.patch.object(telegram_outbound.time, "sleep", lambda _s: None):
                with self.assertRaises(RuntimeError):
                    send_text(
                        instance_dir=Path(tmp),
                        token="tok",
                        response="hello",
                        meta={"chat_id": "1"},
                        log=lambda _m: None,
                    )


class SendPhotoResponseCheckTests(unittest.TestCase):
    def test_api_failure_logged_and_returns_none(self):
        logs: list[str] = []

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": false, "error_code": 400, "description": "PHOTO_INVALID"}'

        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "img.png"
            img.write_bytes(b"\x89PNG fake")
            with mock.patch.object(
                telegram_outbound.urllib.request, "urlopen", lambda *a, **k: _FakeResp()
            ):
                out = telegram_outbound.send_photo(
                    instance_dir=Path(tmp),
                    token="tok",
                    image_path=str(img),
                    meta={"chat_id": "1"},
                    log=logs.append,
                )
        self.assertIsNone(out)
        self.assertTrue(any("sendPhoto failed" in m for m in logs))


class _DriveDone(BaseException):
    pass


class OffsetRewindTests(unittest.TestCase):
    """Enqueue failure must rewind the offset instead of dropping the message."""

    def _channel(self, instance: Path) -> TelegramChannel:
        (instance / "ops").mkdir(exist_ok=True)
        (instance / "ops" / "gateway.yaml").write_text(
            "default_brain: claude\n"
            "channels:\n"
            "  telegram:\n"
            "    enabled: true\n"
            "    token_env: TELEGRAM_BOT_TOKEN\n"
            "    chat_ids: [28547271]\n"
        )
        gateway_config.clear_config_cache()
        cfg = ChannelConfig(
            enabled=True, token_env="TELEGRAM_BOT_TOKEN", chat_ids=("28547271",)
        )
        channel = TelegramChannel(instance, cfg, lambda _m: None)
        channel.token = "test-token"
        return channel

    def _update(self):
        return {
            "update_id": 500,
            "message": {
                "message_id": 9,
                "chat": {"id": 28547271, "type": "private"},
                "from": {"id": 28547271, "username": "luca"},
                "text": "hello there",
            },
        }

    def test_transient_enqueue_failure_re_polls_same_update(self):
        offsets: list = []
        enqueued: list = []
        attempts = {"n": 0}

        def enqueue(**kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("database is locked")
            enqueued.append(kwargs)

        def fake_http_json(url, **kw):
            if "getUpdates" in url:
                if len(offsets) >= 4 or enqueued:
                    raise _DriveDone()
                import urllib.parse as _up

                qs = _up.parse_qs(_up.urlparse(url).query)
                offsets.append(int(qs.get("offset", ["0"])[0]))
                return {"ok": True, "result": [self._update()]}
            return {"ok": True, "result": {}}

        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp))
            logs: list[str] = []
            channel.log = logs.append
            with mock.patch.object(telegram_module, "http_json", fake_http_json):
                try:
                    channel.run(enqueue=enqueue, should_stop=lambda: False)
                except _DriveDone:
                    pass
        self.assertEqual(len(enqueued), 1, f"logs={logs}")
        # Second poll must re-request the SAME update (offset rewound to 500),
        # not 501 (which would have dropped it).
        self.assertGreaterEqual(len(offsets), 2)
        self.assertEqual(offsets[1], 500)

    def test_poison_update_dropped_after_three_attempts(self):
        offsets: list = []

        def enqueue(**kwargs):
            raise RuntimeError("permanently poisonous")

        def fake_http_json(url, **kw):
            if "getUpdates" in url:
                if len(offsets) >= 5:
                    raise _DriveDone()
                import urllib.parse as _up

                qs = _up.parse_qs(_up.urlparse(url).query)
                offsets.append(int(qs.get("offset", ["0"])[0]))
                return {"ok": True, "result": [self._update()]}
            return {"ok": True, "result": {}}

        with tempfile.TemporaryDirectory() as tmp:
            channel = self._channel(Path(tmp))
            logs: list[str] = []
            channel.log = logs.append
            with mock.patch.object(telegram_module, "http_json", fake_http_json):
                try:
                    channel.run(enqueue=enqueue, should_stop=lambda: False)
                except _DriveDone:
                    pass
        # Attempts 1+2 rewind (offset 500), attempt 3 drops → offset 501.
        self.assertIn(501, offsets)
        self.assertTrue(any("DROPPING" in m for m in logs))


if __name__ == "__main__":
    unittest.main()
