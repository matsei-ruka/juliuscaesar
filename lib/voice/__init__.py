"""JuliusCaesar voice — DashScope Qwen TTS + ASR + enrollment.

Library functions take explicit parameters; the CLI (bin/jc-voice) does
all instance-aware wiring (loads voice.json, loads .env from the instance,
then calls these with the values).

Requirements:
- DASHSCOPE_API_KEY in env (caller loads the instance's .env before calling)
- ffmpeg on PATH
- Python deps: dashscope, requests
"""

from .asr import transcribe
from .enroll import enroll
from .synth import synthesize

__all__ = ["synthesize", "transcribe", "enroll"]
