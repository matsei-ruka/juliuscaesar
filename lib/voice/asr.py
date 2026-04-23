"""Audio transcription via DashScope qwen2.5-omni (multimodal).

Ported from rachel_zane/voice/scripts/transcribe.py. The OpenAI Whisper
path used in the original Rachel setup was dead (401 since early March);
Qwen omni replaces it and works with the same DASHSCOPE_API_KEY.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path

import requests


URL_INTL = "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
DEFAULT_MODEL = "qwen2.5-omni-7b"
DEFAULT_PROMPT = (
    "Transcribe this audio verbatim in the spoken language. "
    "Output only the transcription, no commentary."
)


def transcribe(
    audio_path: Path,
    *,
    model: str = DEFAULT_MODEL,
    prompt: str = DEFAULT_PROMPT,
    url: str = URL_INTL,
    timeout_s: float = 120.0,
) -> str:
    """Return the transcribed text. Raises on API failure."""
    audio_path = Path(audio_path).expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(f"audio file not found: {audio_path}")

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DASHSCOPE_API_KEY in env")

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
        raise RuntimeError(f"transcription failed: {r.status_code} {r.text[:500]}")
    data = r.json()
    parts = data["output"]["choices"][0]["message"]["content"]
    return "".join(p.get("text", "") for p in parts).strip()
