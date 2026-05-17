"""Video visual description via DashScope qwen3.6-plus (vision-language model).

Pairs with `voice.asr` to split video ingestion into two artifacts: audio
transcript (omni model) + visual description (this module). The omni model
confabulates visual detail; the plus/vl models confabulate speech. Split,
then fuse downstream.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

import requests

from gateway.config import env_value


INTL_ENDPOINT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_VLM_MODEL = "qwen3.6-plus"
DEFAULT_PROMPT = """Describe the visual content of this video. Cover:
- Subject(s): appearance, clothing, expression.
- Setting: background, lighting, notable objects.
- Action: camera movement, gestures, key actions, exact finger count.

Do NOT transcribe the audio. Do NOT guess what is being said.
Output: 2-4 sentence prose paragraph in English."""


def describe_video(
    video_path: Path,
    *,
    instance_dir: Path,
    model: str = DEFAULT_VLM_MODEL,
    prompt: str = DEFAULT_PROMPT,
    endpoint: str = INTL_ENDPOINT,
    timeout_s: float = 120.0,
) -> str:
    """Return a prose visual description of the video. Raises on API failure."""
    video_path = Path(video_path).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"video file not found: {video_path}")

    api_key = env_value(instance_dir, "DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DASHSCOPE_API_KEY in instance .env or env")

    mime = mimetypes.guess_type(str(video_path))[0] or "video/mp4"
    data_uri = f"data:{mime};base64,{base64.b64encode(video_path.read_bytes()).decode()}"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": data_uri}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    r = requests.post(
        endpoint,
        json=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout_s,
    )
    if r.status_code != 200:
        raise RuntimeError(f"video description failed: {r.status_code} {r.text[:500]}")
    data = r.json()
    return str(data["choices"][0]["message"]["content"]).strip()
