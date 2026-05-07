"""Voice calls must resolve DashScope credentials from the target instance."""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.config import clear_env_cache  # noqa: E402
from voice import asr, synth  # noqa: E402


class VoiceEnvLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_env_cache()

    def tearDown(self) -> None:
        clear_env_cache()

    def test_asr_prefers_instance_env_over_process_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text(
                "DASHSCOPE_API_KEY=instance-key\n",
                encoding="utf-8",
            )
            audio = instance / "sample.ogg"
            audio.write_bytes(b"OggS")
            captured: dict[str, object] = {}

            class FakeResponse:
                status_code = 200
                text = ""

                def json(self):
                    return {
                        "output": {
                            "choices": [
                                {
                                    "message": {
                                        "content": [
                                            {"text": "ciao"},
                                        ]
                                    }
                                }
                            ]
                        }
                    }

            def fake_post(url, *, json, headers, timeout):
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers
                captured["timeout"] = timeout
                return FakeResponse()

            with mock.patch.object(asr.requests, "post", side_effect=fake_post), \
                 mock.patch.dict(
                     os.environ,
                     {"DASHSCOPE_API_KEY": "process-key"},
                     clear=False,
                 ):
                text = asr.transcribe(audio, instance_dir=instance)

            self.assertEqual(text, "ciao")
            self.assertEqual(captured["headers"]["Authorization"], "Bearer instance-key")

    def test_synth_prefers_instance_env_over_process_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text(
                "DASHSCOPE_API_KEY=instance-key\n",
                encoding="utf-8",
            )
            pcm = instance / "out.pcm"
            calls: dict[str, object] = {}
            fake_dashscope = types.ModuleType("dashscope")
            fake_audio = types.ModuleType("dashscope.audio")
            fake_qwen = types.ModuleType("dashscope.audio.qwen_tts_realtime")

            class AudioFormat:
                PCM_24000HZ_MONO_16BIT = "pcm"

            class QwenTtsRealtimeCallback:
                pass

            class QwenTtsRealtime:
                def __init__(self, *, model, callback, url):
                    calls["model"] = model
                    calls["url"] = url
                    self.callback = callback

                def connect(self):
                    calls["connected"] = True

                def update_session(self, **kwargs):
                    calls["session"] = kwargs

                def append_text(self, text):
                    calls["text"] = text

                def finish(self):
                    delta = base64.b64encode(b"pcm-data").decode()
                    self.callback.on_event({"type": "response.audio.delta", "delta": delta})
                    self.callback.on_event({"type": "session.finished"})

            fake_qwen.AudioFormat = AudioFormat
            fake_qwen.QwenTtsRealtime = QwenTtsRealtime
            fake_qwen.QwenTtsRealtimeCallback = QwenTtsRealtimeCallback

            modules = {
                "dashscope": fake_dashscope,
                "dashscope.audio": fake_audio,
                "dashscope.audio.qwen_tts_realtime": fake_qwen,
            }
            with mock.patch.dict(sys.modules, modules), \
                 mock.patch.dict(
                     os.environ,
                     {"DASHSCOPE_API_KEY": "process-key"},
                     clear=False,
                 ):
                synth._synthesize_pcm(
                    "hello",
                    instance_dir=instance,
                    voice_id="voice-1",
                    target_model="model-1",
                    ws_url="wss://example.test",
                    pcm_path=pcm,
                )

            self.assertEqual(fake_dashscope.api_key, "instance-key")
            self.assertEqual(calls["text"], "hello")
            self.assertEqual(pcm.read_bytes(), b"pcm-data")


if __name__ == "__main__":
    unittest.main()
