"""JuliusCaesar voice — DashScope Qwen TTS + ASR + enrollment.

ASR/TTS functions take an explicit instance_dir so each instance resolves
DASHSCOPE_API_KEY from its own .env before falling back to process env. The CLI
still applies the instance .env for enrollment/listing helpers.

Requirements:
- DASHSCOPE_API_KEY in the instance .env or process env
- ffmpeg on PATH
- Python deps: dashscope, requests
"""

from .asr import transcribe
from .enroll import enroll
from .synth import synthesize

__all__ = ["synthesize", "transcribe", "enroll"]
