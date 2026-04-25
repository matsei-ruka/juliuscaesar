"""DashScope Qwen TTS realtime → PCM → OGG/Opus.

The caller passes voice_id + target_model explicitly; this module only handles
the TTS realtime stream and OGG/Opus conversion.
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import threading
from pathlib import Path


WS_URL_INTL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"


def _check_deps() -> None:
    try:
        import dashscope  # noqa: F401
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "dashscope SDK not installed. Install with: pip install -U dashscope"
        ) from e
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # pragma: no cover
        raise RuntimeError("ffmpeg not available on PATH") from e


def _synthesize_pcm(
    text: str,
    *,
    voice_id: str,
    target_model: str,
    ws_url: str,
    pcm_path: Path,
    timeout_s: float = 120.0,
) -> None:
    import dashscope
    from dashscope.audio.qwen_tts_realtime import (
        AudioFormat,
        QwenTtsRealtime,
        QwenTtsRealtimeCallback,
    )

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DASHSCOPE_API_KEY in env")
    dashscope.api_key = api_key

    class CB(QwenTtsRealtimeCallback):
        def __init__(self):
            self.done = threading.Event()
            self.f = open(pcm_path, "wb")

        def on_close(self, close_status_code, close_msg) -> None:
            try:
                self.f.close()
            finally:
                self.done.set()

        def on_event(self, response: dict) -> None:
            t = response.get("type")
            if t == "response.audio.delta":
                self.f.write(base64.b64decode(response["delta"]))
            elif t == "session.finished":
                try:
                    self.f.close()
                finally:
                    self.done.set()

    cb = CB()
    tts = QwenTtsRealtime(model=target_model, callback=cb, url=ws_url)
    tts.connect()
    tts.update_session(
        voice=voice_id,
        response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
        mode="server_commit",
    )
    tts.append_text(text)
    tts.finish()
    cb.done.wait(timeout=timeout_s)


def _pcm_to_ogg(pcm_path: Path, ogg_path: Path) -> None:
    """Raw PCM s16le @ 24kHz mono → OGG/Opus (Telegram-voice compatible)."""
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "s16le", "-ar", "24000", "-ac", "1",
        "-i", str(pcm_path),
        "-c:a", "libopus", "-b:a", "24k",
        "-vbr", "on", "-application", "voip",
        str(ogg_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def synthesize(
    text: str,
    out_path: Path,
    *,
    voice_id: str,
    target_model: str,
    ws_url: str = WS_URL_INTL,
) -> Path:
    """Synthesize `text` with the given cloned voice; write OGG/Opus to `out_path`.

    Returns the output path. Raises on failure.
    """
    _check_deps()
    out_path = out_path.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        pcm = Path(td) / "out.pcm"
        _synthesize_pcm(
            text,
            voice_id=voice_id,
            target_model=target_model,
            ws_url=ws_url,
            pcm_path=pcm,
        )
        _pcm_to_ogg(pcm, out_path)
    return out_path
