"""Telegram media download and ingestion helpers."""

from __future__ import annotations

import mimetypes
import shutil
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ._http import http_json


AUDIO_MIME_EXT = {
    "audio/ogg": ".oga",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/webm": ".webm",
    "audio/x-wav": ".wav",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
}

# Backwards-compat alias for older voice-specific callers.
VOICE_MIME_EXT = AUDIO_MIME_EXT


def download_telegram_file(
    token: str,
    file_id: str,
    dest: Path,
    *,
    timeout: int = 60,
) -> Path:
    """Resolve Telegram `file_id` via getFile, then stream the bytes to `dest`."""
    info = http_json(
        f"https://api.telegram.org/bot{token}/getFile?"
        + urllib.parse.urlencode({"file_id": file_id}),
        timeout=timeout,
    )
    if not info.get("ok"):
        raise RuntimeError(f"telegram getFile failed: {info}")
    file_path = (info.get("result") or {}).get("file_path")
    if not file_path:
        raise RuntimeError(f"telegram getFile missing file_path: {info}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out)
    return dest


def transcribe_audio(audio_path: Path) -> str:
    """Best-effort ASR via `voice.asr.transcribe`. Returns "" on failure."""
    from importlib import import_module

    mod = import_module("voice.asr")
    return str(mod.transcribe(audio_path)).strip()


def ingest_audio_attachment(
    *,
    token: str,
    instance_dir: Path,
    payload: dict[str, Any],
    kind: str | None,
    update_id: Any,
) -> Path:
    """Download a transcribable Telegram attachment to `state/voice/inbound/`."""
    file_id = payload.get("file_id")
    if not file_id:
        raise RuntimeError(f"{kind or 'attachment'} payload missing file_id")
    if kind == "video_note":
        ext = ".mp4"
    else:
        ext = AUDIO_MIME_EXT.get(str(payload.get("mime_type") or ""), ".oga")
    dest = instance_dir / "state" / "voice" / "inbound" / f"{update_id}{ext}"
    return download_telegram_file(token, file_id, dest)


def ingest_photo(
    *,
    token: str,
    instance_dir: Path,
    photos: list[Any],
    update_id: Any,
) -> Path:
    """Download the largest photo size to `state/voice/inbound/photos/`."""
    if not photos:
        raise RuntimeError("photo payload empty")
    largest = photos[-1]
    if not isinstance(largest, dict):
        raise RuntimeError("photo payload malformed")
    file_id = largest.get("file_id")
    if not file_id:
        raise RuntimeError("photo payload missing file_id")
    dest = instance_dir / "state" / "voice" / "inbound" / "photos" / f"{update_id}.jpg"
    return download_telegram_file(token, file_id, dest)


def ingest_document(
    *,
    token: str,
    instance_dir: Path,
    document: dict[str, Any],
    update_id: Any,
) -> Path:
    """Download a Telegram document to `state/voice/inbound/docs/`."""
    file_id = document.get("file_id")
    if not file_id:
        raise RuntimeError("document payload missing file_id")
    original = document.get("file_name") or ""
    ext = Path(original).suffix
    if not ext:
        mime = str(document.get("mime_type") or "")
        ext = mimetypes.guess_extension(mime) or ".bin"
    dest = instance_dir / "state" / "voice" / "inbound" / "docs" / f"{update_id}{ext}"
    return download_telegram_file(token, file_id, dest)
