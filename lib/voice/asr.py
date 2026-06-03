"""Audio transcription via DashScope qwen2.5-omni (multimodal).

DashScope qwen omni handles transcription and works with the same
DASHSCOPE_API_KEY used by the rest of the voice subsystem.

DashScope hard-caps audio at ~60s. For longer clips it returns
400 InvalidParameter "The audio is too long". On that specific
symptom we fall back to OpenAI Whisper (whisper-1) for the one call.
Any other failure mode preserves prior behavior (raise).
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path

import requests

from gateway.config import env_value


URL_INTL = "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
DEFAULT_MODEL = "qwen2.5-omni-7b"
DEFAULT_PROMPT = (
    "Transcribe this audio verbatim in the spoken language. "
    "Output only the transcription, no commentary."
)

WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-1"
FALLBACK_TRIGGERS = ("audio is too long", "InvalidParameter")

logger = logging.getLogger(__name__)


def _transcribe_whisper(
    audio_path: Path,
    *,
    instance_dir: Path,
    timeout_s: float,
) -> str:
    """POST raw audio bytes to OpenAI Whisper, multipart. Returns text.

    Raises RuntimeError on missing key or any HTTP/transport failure.
    """
    api_key = env_value(instance_dir, "OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in instance .env or env")

    mime = mimetypes.guess_type(str(audio_path))[0] or "audio/ogg"
    r = requests.post(
        WHISPER_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (audio_path.name, audio_path.read_bytes(), mime)},
        data={"model": WHISPER_MODEL, "response_format": "text"},
        timeout=timeout_s,
    )
    if r.status_code != 200:
        raise RuntimeError(f"whisper failed: {r.status_code} {r.text[:500]}")
    return r.text.strip()


def transcribe(
    audio_path: Path,
    *,
    instance_dir: Path,
    model: str = DEFAULT_MODEL,
    prompt: str = DEFAULT_PROMPT,
    url: str = URL_INTL,
    timeout_s: float = 120.0,
) -> str:
    """Return the transcribed text. Raises on API failure."""
    audio_path = Path(audio_path).expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(f"audio file not found: {audio_path}")

    api_key = env_value(instance_dir, "DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DASHSCOPE_API_KEY in instance .env or env")

    mime = mimetypes.guess_type(str(audio_path))[0] or "audio/ogg"
    data_uri = f"data:{mime};base64,{base64.b64encode(audio_path.read_bytes()).decode()}"

    payload = {
        "model": model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [{"audio": data_uri}, {"text": prompt}],
                }
            ]
        },
    }
    r = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=timeout_s,
    )
    if r.status_code != 200:
        original_error = RuntimeError(
            f"transcription failed: {r.status_code} {r.text[:500]}"
        )
        body = r.text or ""
        reason = None
        if "audio is too long" in body:
            reason = "audio_too_long"
        elif "InvalidParameter" in body:
            reason = "invalid_parameter"
        if reason is not None:
            logger.info("asr fallback: dashscope->whisper reason=%s", reason)
            try:
                return _transcribe_whisper(
                    audio_path,
                    instance_dir=instance_dir,
                    timeout_s=timeout_s,
                )
            except Exception as whisper_err:
                logger.warning("asr fallback whisper failed: %s", whisper_err)
                raise original_error from whisper_err
        raise original_error
    data = r.json()
    parts = data["output"]["choices"][0]["message"]["content"]
    return "".join(p.get("text", "") for p in parts).strip()
