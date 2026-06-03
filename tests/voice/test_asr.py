"""Tests for DashScope ASR with OpenAI Whisper fallback on long audio."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.config import clear_env_cache  # noqa: E402
from voice import asr  # noqa: E402


def _make_audio(tmp_path: Path) -> Path:
    p = tmp_path / "msg.ogg"
    p.write_bytes(b"\x00\x01\x02fake-ogg-bytes")
    return p


def _make_instance(tmp_path: Path, *, openai_key: str | None = "sk-openai-test") -> Path:
    instance = tmp_path / "instance"
    instance.mkdir()
    lines = ["DASHSCOPE_API_KEY=ds-test\n"]
    if openai_key:
        lines.append(f"OPENAI_API_KEY={openai_key}\n")
    (instance / ".env").write_text("".join(lines), encoding="utf-8")
    return instance


class _Resp:
    def __init__(self, status_code: int, *, text: str = "", json_body=None):
        self.status_code = status_code
        self.text = text
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def _dashscope_ok(text: str = "ciao") -> _Resp:
    return _Resp(
        200,
        json_body={
            "output": {
                "choices": [
                    {"message": {"content": [{"text": text}]}}
                ]
            }
        },
    )


class AsrFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_env_cache()
        self._tmpdir = Path(__file__).resolve().parent  # placeholder for type checkers

    def tearDown(self) -> None:
        clear_env_cache()

    # ------------------------------------------------------------------
    # 1. DashScope succeeds — no fallback
    # ------------------------------------------------------------------
    def test_dashscope_success_no_fallback(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            instance = _make_instance(tmp_path)
            audio = _make_audio(tmp_path)

            calls: list[dict] = []

            def fake_post(url, *args, **kwargs):
                calls.append({"url": url, "kwargs": kwargs})
                return _dashscope_ok("ciao")

            with mock.patch.object(asr.requests, "post", side_effect=fake_post):
                text = asr.transcribe(audio, instance_dir=instance)

            self.assertEqual(text, "ciao")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["url"], asr.URL_INTL)

    # ------------------------------------------------------------------
    # 2. DashScope 400 "audio is too long" → Whisper succeeds
    # ------------------------------------------------------------------
    def test_fallback_on_audio_too_long_whisper_succeeds(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            instance = _make_instance(tmp_path)
            audio = _make_audio(tmp_path)

            calls: list[dict] = []

            def fake_post(url, *args, **kwargs):
                calls.append({"url": url, "kwargs": kwargs})
                if url == asr.URL_INTL:
                    return _Resp(
                        400,
                        text='{"code":"InvalidParameter","message":"The audio is too long"}',
                    )
                if url == asr.WHISPER_URL:
                    return _Resp(200, text="long audio transcript\n")
                raise AssertionError(f"unexpected url: {url}")

            with mock.patch.object(asr.requests, "post", side_effect=fake_post):
                text = asr.transcribe(audio, instance_dir=instance)

            self.assertEqual(text, "long audio transcript")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["url"], asr.URL_INTL)
            self.assertEqual(calls[1]["url"], asr.WHISPER_URL)

            whisper_kwargs = calls[1]["kwargs"]
            self.assertIn("files", whisper_kwargs)
            self.assertIn("file", whisper_kwargs["files"])
            self.assertEqual(whisper_kwargs["data"]["model"], "whisper-1")
            self.assertEqual(whisper_kwargs["data"]["response_format"], "text")
            self.assertEqual(
                whisper_kwargs["headers"]["Authorization"],
                "Bearer sk-openai-test",
            )
            self.assertNotIn("Content-Type", whisper_kwargs["headers"])

    # ------------------------------------------------------------------
    # 3. DashScope 400 InvalidParameter (no "audio is too long") → Whisper
    # ------------------------------------------------------------------
    def test_fallback_on_invalid_parameter_whisper_succeeds(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            instance = _make_instance(tmp_path)
            audio = _make_audio(tmp_path)

            calls: list[dict] = []

            def fake_post(url, *args, **kwargs):
                calls.append({"url": url, "kwargs": kwargs})
                if url == asr.URL_INTL:
                    return _Resp(
                        400,
                        text='{"code":"InvalidParameter","message":"bad arg"}',
                    )
                if url == asr.WHISPER_URL:
                    return _Resp(200, text="whisper text")
                raise AssertionError(f"unexpected url: {url}")

            with mock.patch.object(asr.requests, "post", side_effect=fake_post):
                text = asr.transcribe(audio, instance_dir=instance)

            self.assertEqual(text, "whisper text")
            self.assertEqual(len(calls), 2)
            whisper_kwargs = calls[1]["kwargs"]
            self.assertIn("files", whisper_kwargs)
            self.assertEqual(whisper_kwargs["data"]["model"], "whisper-1")
            self.assertEqual(whisper_kwargs["data"]["response_format"], "text")
            self.assertEqual(
                whisper_kwargs["headers"]["Authorization"],
                "Bearer sk-openai-test",
            )

    # ------------------------------------------------------------------
    # 4. Trigger fires but OPENAI_API_KEY missing → raise ORIGINAL DashScope error
    # ------------------------------------------------------------------
    def test_fallback_but_no_openai_key_raises_original(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            instance = _make_instance(tmp_path, openai_key=None)
            audio = _make_audio(tmp_path)

            calls: list[dict] = []

            def fake_post(url, *args, **kwargs):
                calls.append({"url": url, "kwargs": kwargs})
                if url == asr.URL_INTL:
                    return _Resp(
                        400,
                        text='{"code":"InvalidParameter","message":"The audio is too long"}',
                    )
                raise AssertionError(f"whisper must NOT be called, got url={url}")

            with mock.patch.object(asr.requests, "post", side_effect=fake_post):
                with self.assertRaises(RuntimeError) as ctx:
                    asr.transcribe(audio, instance_dir=instance)

            self.assertTrue(
                str(ctx.exception).startswith("transcription failed: 400"),
                f"unexpected error: {ctx.exception!s}",
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["url"], asr.URL_INTL)

    # ------------------------------------------------------------------
    # 5. Trigger fires, Whisper 500 → raise ORIGINAL DashScope error
    # ------------------------------------------------------------------
    def test_fallback_but_whisper_500_raises_original(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            instance = _make_instance(tmp_path)
            audio = _make_audio(tmp_path)

            def fake_post(url, *args, **kwargs):
                if url == asr.URL_INTL:
                    return _Resp(
                        400,
                        text='{"code":"InvalidParameter","message":"The audio is too long"}',
                    )
                if url == asr.WHISPER_URL:
                    return _Resp(500, text="internal error")
                raise AssertionError(f"unexpected url: {url}")

            with mock.patch.object(asr.requests, "post", side_effect=fake_post):
                with self.assertRaises(RuntimeError) as ctx:
                    asr.transcribe(audio, instance_dir=instance)

            msg = str(ctx.exception)
            self.assertTrue(
                msg.startswith("transcription failed: 400"),
                f"unexpected error: {msg!r}",
            )
            self.assertNotIn("whisper failed", msg)

    # ------------------------------------------------------------------
    # 6. DashScope 401 (auth) — no fallback
    # ------------------------------------------------------------------
    def test_dashscope_auth_failure_no_fallback(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            instance = _make_instance(tmp_path)
            audio = _make_audio(tmp_path)

            calls: list[dict] = []

            def fake_post(url, *args, **kwargs):
                calls.append({"url": url})
                if url == asr.URL_INTL:
                    return _Resp(401, text="Unauthorized")
                raise AssertionError(f"whisper must NOT be called, got url={url}")

            with mock.patch.object(asr.requests, "post", side_effect=fake_post):
                with self.assertRaises(RuntimeError) as ctx:
                    asr.transcribe(audio, instance_dir=instance)

            self.assertTrue(
                str(ctx.exception).startswith("transcription failed: 401"),
                f"unexpected error: {ctx.exception!s}",
            )
            self.assertEqual(len(calls), 1)

    # ------------------------------------------------------------------
    # 7. DashScope 500 — no fallback (no trigger substring)
    # ------------------------------------------------------------------
    def test_dashscope_500_no_fallback(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            instance = _make_instance(tmp_path)
            audio = _make_audio(tmp_path)

            calls: list[dict] = []

            def fake_post(url, *args, **kwargs):
                calls.append({"url": url})
                if url == asr.URL_INTL:
                    return _Resp(500, text="Internal Server Error")
                raise AssertionError(f"whisper must NOT be called, got url={url}")

            with mock.patch.object(asr.requests, "post", side_effect=fake_post):
                with self.assertRaises(RuntimeError) as ctx:
                    asr.transcribe(audio, instance_dir=instance)

            self.assertTrue(
                str(ctx.exception).startswith("transcription failed: 500"),
                f"unexpected error: {ctx.exception!s}",
            )
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
