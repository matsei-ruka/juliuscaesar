"""Telegram outbound sending helpers."""

from __future__ import annotations

import json
import mimetypes
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from ..config import env_value
from ..format import to_markdown_v2
from ._http import http_json
from .base import LogFn


def send_typing(*, token: str, chat_id: str, message_thread_id: int | None = None) -> None:
    """POST `sendChatAction` with `action=typing`. Best-effort; no return."""
    if not token or not chat_id:
        return
    payload: dict[str, Any] = {"chat_id": str(chat_id), "action": "typing"}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    http_json(
        f"https://api.telegram.org/bot{token}/sendChatAction",
        data=payload,
        timeout=10,
    )


def send_text(
    *,
    instance_dir: Path,
    token: str,
    response: str,
    meta: dict[str, Any],
    log: LogFn,
) -> str | None:
    if not token or not response.strip():
        return None
    chat_id = str(
        meta.get("chat_id")
        or meta.get("notify_chat_id")
        or env_value(instance_dir, "TELEGRAM_CHAT_ID")
        or ""
    )
    if not chat_id:
        return None
    original = response[:4096]
    escaped = to_markdown_v2(original)
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": escaped,
        "disable_web_page_preview": True,
        "parse_mode": "MarkdownV2",
    }
    if meta.get("message_thread_id"):
        payload["message_thread_id"] = meta["message_thread_id"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        data = http_json(url, data=payload, timeout=15)
        parse_error = (
            not data.get("ok")
            and "parse" in str(data.get("description") or "").lower()
        )
        error_desc = str(data.get("description") or "")
    except HTTPError as exc:
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
            body = json.loads(body_text) if body_text else {}
        except (json.JSONDecodeError, ValueError):
            body = {}
        error_desc = str(body.get("description") or exc.reason or "")
        parse_error = exc.code == 400 and (
            "parse" in error_desc.lower() or "entit" in error_desc.lower()
        )
        data = body if isinstance(body, dict) else {}
        if not parse_error:
            raise RuntimeError(f"telegram send failed: HTTP {exc.code} {error_desc}") from exc
    if parse_error:
        log(f"telegram.send.parse_error retrying without parse_mode err={error_desc!r}")
        fallback: dict[str, Any] = {
            "chat_id": chat_id,
            "text": original,
            "disable_web_page_preview": True,
        }
        if meta.get("message_thread_id"):
            fallback["message_thread_id"] = meta["message_thread_id"]
        data = http_json(url, data=fallback, timeout=15)
    if not data.get("ok"):
        raise RuntimeError(f"telegram send failed: {data}")
    result = data.get("result") or {}
    return str(result.get("message_id")) if result.get("message_id") is not None else None


def send_voice(
    *,
    instance_dir: Path,
    token: str,
    ogg_path: str,
    meta: dict[str, Any],
) -> str | None:
    """Upload an OGG/Opus file and post it as a Telegram voice message."""
    if not token:
        return None
    chat_id = str(
        meta.get("chat_id")
        or meta.get("notify_chat_id")
        or env_value(instance_dir, "TELEGRAM_CHAT_ID")
        or ""
    )
    if not chat_id:
        return None
    path = Path(ogg_path)
    if not path.exists():
        raise RuntimeError(f"telegram sendVoice missing file: {ogg_path}")

    fields: list[tuple[str, str]] = [("chat_id", chat_id)]
    if meta.get("message_thread_id"):
        fields.append(("message_thread_id", str(meta["message_thread_id"])))
    files: list[tuple[str, str, bytes, str]] = [
        ("voice", path.name, path.read_bytes(), "audio/ogg"),
    ]
    body, content_type = encode_multipart(fields, files)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendVoice",
        data=body,
        headers={"Content-Type": content_type},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw) if raw else {}
    if not data.get("ok"):
        raise RuntimeError(f"telegram sendVoice failed: {data}")
    result = data.get("result") or {}
    return str(result.get("message_id")) if result.get("message_id") is not None else None


def encode_multipart(
    fields: list[tuple[str, str]],
    files: list[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    """Build a `multipart/form-data` body for urllib."""
    boundary = f"----jcboundary{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields:
        parts.append(f"--{boundary}".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"))
        parts.append(b"")
        parts.append(value.encode("utf-8"))
    for name, filename, data, content_type in files:
        parts.append(f"--{boundary}".encode("utf-8"))
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode(
                "utf-8"
            )
        )
        parts.append(
            f"Content-Type: {content_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'}".encode(
                "utf-8"
            )
        )
        parts.append(b"")
        parts.append(data)
    parts.append(f"--{boundary}--".encode("utf-8"))
    parts.append(b"")
    body = crlf.join(parts)
    return body, f"multipart/form-data; boundary={boundary}"
