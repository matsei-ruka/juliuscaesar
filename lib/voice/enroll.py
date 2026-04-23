"""DashScope Qwen voice enrollment (clone a sample → persistent voice id).

Ported from rachel_zane/voice/scripts/enroll_voice.py. Returns a dict
containing {voice, target_model, preferred_name}; caller is responsible
for persisting (typically to <instance>/voice/references/voice.json).
"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path

import requests


CUSTOMIZATION_URL_INTL = (
    "https://dashscope-intl.aliyuncs.com/api/v1/services/audio/tts/customization"
)
DEFAULT_TARGET_MODEL = "qwen3-tts-vc-realtime-2026-01-15"


def enroll(
    audio_path: Path,
    *,
    preferred_name: str = "rachel",
    target_model: str = DEFAULT_TARGET_MODEL,
    url: str = CUSTOMIZATION_URL_INTL,
    timeout_s: float = 120.0,
) -> dict:
    """Enroll a sample into a persistent DashScope voice. Returns metadata dict."""
    audio_path = Path(audio_path).expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(f"audio sample not found: {audio_path}")

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DASHSCOPE_API_KEY in env")

    mime = mimetypes.guess_type(str(audio_path))[0] or "audio/mpeg"
    b64 = base64.b64encode(audio_path.read_bytes()).decode()
    data_uri = f"data:{mime};base64,{b64}"

    payload = {
        "model": "qwen-voice-enrollment",
        "input": {
            "action": "create",
            "target_model": target_model,
            "preferred_name": preferred_name,
            "audio": {"data": data_uri},
        },
    }

    r = requests.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout_s,
    )
    if r.status_code != 200:
        raise RuntimeError(f"enrollment failed: {r.status_code} {r.text[:500]}")

    data = r.json()
    voice = data["output"]["voice"]
    return {
        "voice": voice,
        "target_model": target_model,
        "preferred_name": preferred_name,
    }


def list_voices(
    *,
    url: str = CUSTOMIZATION_URL_INTL,
    page_size: int = 50,
    timeout_s: float = 30.0,
) -> list[dict]:
    """List enrolled voices on the DashScope account tied to DASHSCOPE_API_KEY."""
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DASHSCOPE_API_KEY in env")

    r = requests.post(
        url,
        json={"model": "qwen-voice-enrollment", "input": {"action": "list", "page_size": page_size}},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=timeout_s,
    )
    if r.status_code != 200:
        raise RuntimeError(f"list failed: {r.status_code} {r.text[:500]}")
    return r.json()["output"]["voice_list"]
