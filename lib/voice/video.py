"""Video ingestion — split-and-fuse (audio → ASR, frames → VLM).

Empirical reality (see docs/specs/video-ingestion.md): no single Qwen model
does both jobs well — omni hears but mis-sees, plus/vl sees but confabulates
audio. Strip audio for ASR, hand the full file to the VLM, fuse downstream.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import asr, vlm


MAX_BYTES = 50 * 1024 * 1024


def _log(msg: str) -> None:
    print(f"[voice.video] {msg}", file=sys.stderr, flush=True)


def ingest_video(
    video_path: Path,
    *,
    instance_dir: Path,
    vlm_model: str | None = None,
    transcript_when_silent: str = "",
) -> tuple[str, str]:
    """Return ``(transcript, visual)``. Either may be empty on failure."""
    video_path = Path(video_path).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"video file not found: {video_path}")
    size = os.path.getsize(video_path)
    if size > MAX_BYTES:
        raise ValueError(f"video too large (>50 MB): {size} bytes")

    transcript = _extract_transcript(
        video_path,
        instance_dir=instance_dir,
        transcript_when_silent=transcript_when_silent,
    )

    visual = ""
    try:
        visual = vlm.describe_video(
            video_path,
            instance_dir=instance_dir,
            model=vlm_model or vlm.DEFAULT_VLM_MODEL,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"vlm.describe_video failed: {exc}")

    return transcript, visual


def _extract_transcript(
    video_path: Path,
    *,
    instance_dir: Path,
    transcript_when_silent: str,
) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_path = Path(tmp.name)
    tmp.close()
    try:
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(video_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(wav_path),
                ],
                check=True,
            )
        except FileNotFoundError:
            _log("ffmpeg not found on PATH; skipping audio strip")
            return transcript_when_silent
        except subprocess.CalledProcessError as exc:
            _log(f"ffmpeg strip failed: {exc}")
            return transcript_when_silent

        try:
            text = asr.transcribe(wav_path, instance_dir=instance_dir).strip()
        except Exception as exc:  # noqa: BLE001
            _log(f"asr.transcribe failed: {exc}")
            return transcript_when_silent
        if not text:
            return transcript_when_silent
        return text
    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass


def fused_video_event_text(transcript: str, visual: str) -> str:
    """Build the brain-prompt injection text for a video event.

    Omits a section when its content is empty so the prompt doesn't carry
    dangling labels.
    """
    parts: list[str] = ["[user sent a video; transcript and visual analysis follow]"]
    if transcript:
        parts.append(f"\nTRANSCRIPT:\n{transcript}")
    if visual:
        parts.append(f"\nVISUAL:\n{visual}")
    return "\n".join(parts)
