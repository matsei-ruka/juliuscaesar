"""Telegram outbound sending helpers."""

from __future__ import annotations

import json
import mimetypes
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from ..config import env_value
from ..format import to_markdown_v2
from ._http import http_json
from .base import LogFn


def _is_session_backgrounded(action_session_id: str) -> bool:
    """Defer-load the registry to avoid a hard import cycle on cold tests."""
    try:
        from .. import actions_registry  # local import to keep this module light
        return actions_registry.is_backgrounded(action_session_id)
    except Exception:  # noqa: BLE001
        return False


def _buffer_for_backgrounded(action_session_id: str, text: str) -> None:
    try:
        from .. import actions_registry
        actions_registry.buffer_tool_message(action_session_id, text)
    except Exception:  # noqa: BLE001
        pass


def send_typing(
    *,
    token: str,
    chat_id: str,
    message_thread_id: int | None = None,
    action: str = "typing",
) -> None:
    """POST `sendChatAction`. Best-effort; no return."""
    if not token or not chat_id:
        return
    payload: dict[str, Any] = {"chat_id": str(chat_id), "action": action}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    http_json(
        f"https://api.telegram.org/bot{token}/sendChatAction",
        data=payload,
        timeout=10,
    )


def set_message_reaction(
    *,
    token: str,
    chat_id: str,
    message_id: int,
    emoji: str | None,
    log: LogFn | None = None,
) -> None:
    """POST `setMessageReaction`. Pass emoji=None to clear the reaction.

    Best-effort — errors are logged (if `log` is provided) but never raised
    so a failed reaction never interrupts normal message flow.
    """
    if not token or not chat_id:
        return
    reaction: list[dict[str, str]] = (
        [{"type": "emoji", "emoji": emoji}] if emoji else []
    )
    try:
        data = http_json(
            f"https://api.telegram.org/bot{token}/setMessageReaction",
            data={"chat_id": str(chat_id), "message_id": message_id, "reaction": reaction},
            timeout=10,
        )
        if log and isinstance(data, dict) and not data.get("ok", False):
            log(
                f"telegram setMessageReaction failed: chat_id={chat_id} "
                f"message_id={message_id} emoji={emoji!r} response={data}"
            )
    except Exception as exc:  # noqa: BLE001
        if log:
            log(
                f"telegram setMessageReaction error: chat_id={chat_id} "
                f"message_id={message_id} emoji={emoji!r}: {exc}"
            )



TELEGRAM_TEXT_LIMIT = 4096

# 429 send handling (audit F-P2 / feature 6): max attempts and the cap on
# how long a single honor-retry_after sleep may block the dispatch thread.
_SEND_RETRY_ATTEMPTS = 3
_SEND_RETRY_AFTER_CAP_SECONDS = 60.0


def _is_fence_line(line: str) -> bool:
    return line.lstrip().startswith("```")


def _blocks(text: str) -> list[str]:
    """Split text into paragraph blocks; a code fence is one atomic block."""
    blocks: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _is_fence_line(line):
            if not in_fence:
                # Flush any paragraph running straight into the fence so the
                # fence stays an atomic block of its own.
                if current:
                    blocks.append("\n".join(current))
                    current = []
                in_fence = True
                current.append(line)
                continue
            current.append(line)
            in_fence = False
            blocks.append("\n".join(current))
            current = []
            continue
        if in_fence:
            current.append(line)
            continue
        if not line.strip():
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _escaped_fits(text: str, limit: int) -> bool:
    return len(to_markdown_v2(text)) <= limit


def _split_oversize_block(block: str, limit: int) -> list[str]:
    """Split a single block that doesn't fit even alone.

    Fence blocks are split by lines and each piece re-wrapped with the
    original opening fence (language tag preserved) + a closing fence, so a
    chunk never ends inside an open fence. A single oversize line falls back
    to hard slices of ``limit // 2`` raw chars (MarkdownV2 escaping at most
    doubles length, so the escaped slice always fits).
    """
    lines = block.split("\n")
    is_fence = bool(lines) and _is_fence_line(lines[0])
    header = ""
    footer = ""
    if is_fence:
        header = lines[0]
        body = lines[1:]
        if body and _is_fence_line(body[-1]):
            body = body[:-1]
        footer = "```"
        lines = body

    def wrap(piece_lines: list[str]) -> str:
        if is_fence:
            return "\n".join([header, *piece_lines, footer])
        return "\n".join(piece_lines)

    pieces: list[str] = []
    current: list[str] = []
    for line in lines:
        candidate = current + [line]
        if _escaped_fits(wrap(candidate), limit):
            current = candidate
            continue
        if current:
            pieces.append(wrap(current))
            current = []
        if _escaped_fits(wrap([line]), limit):
            current = [line]
            continue
        # Single line too long even alone — hard-slice the raw text.
        step = max(1, limit // 2)
        for i in range(0, len(line), step):
            pieces.append(wrap([line[i : i + step]]))
    if current:
        pieces.append(wrap(current))
    return pieces or [block[: limit // 2]]


def split_for_telegram(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split raw reply text so each chunk's MarkdownV2 escape fits ``limit``.

    Audit feature 6: ``response[:4096]`` silently truncated long replies —
    the footer (appended before the slice) was cut first and the slice could
    land mid-entity. Splits on paragraph boundaries, fence-aware; the caller
    sends chunks in order so the footer survives in the final chunk.
    """
    if _escaped_fits(text, limit):
        return [text]
    chunks: list[str] = []
    current = ""
    for block in _blocks(text):
        candidate = f"{current}\n\n{block}" if current else block
        if _escaped_fits(candidate, limit):
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if _escaped_fits(block, limit):
            current = block
            continue
        chunks.extend(_split_oversize_block(block, limit))
    if current:
        chunks.append(current)
    return chunks or [text[: limit // 2]]


def _post_with_retry(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: int,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """``http_json`` with 429 handling (audit feature 6).

    A 429 body arrives as ``{"ok": false, "error_code": 429}`` (http_json
    returns 4xx JSON bodies since 42582e1). Honor ``parameters.retry_after``
    (capped) and retry up to ``_SEND_RETRY_ATTEMPTS`` times; rate-limited
    replies used to be lost outright.
    """
    data: dict[str, Any] = {}
    for attempt in range(1, _SEND_RETRY_ATTEMPTS + 1):
        data = http_json(url, data=payload, timeout=timeout)
        if data.get("ok") or data.get("error_code") != 429:
            return data
        retry_after = (data.get("parameters") or {}).get("retry_after")
        delay = min(
            _SEND_RETRY_AFTER_CAP_SECONDS,
            float(retry_after) if isinstance(retry_after, (int, float)) and retry_after > 0 else 5.0,
        )
        if log:
            log(
                f"telegram send rate-limited (429) attempt={attempt}/"
                f"{_SEND_RETRY_ATTEMPTS} retry_after={retry_after} — "
                f"sleeping {delay:.0f}s"
            )
        if attempt < _SEND_RETRY_ATTEMPTS:
            time.sleep(delay)
    return data


def send_text(
    *,
    instance_dir: Path,
    token: str,
    response: str,
    meta: dict[str, Any],
    log: LogFn,
    action_session_id: str | None = None,
    suppress_if_backgrounded: bool = True,
) -> str | None:
    """Send `response` to Telegram.

    When ``action_session_id`` identifies a backgrounded session AND
    ``suppress_if_backgrounded`` is True, the send is buffered onto the
    action-registry entry (via ``buffer_tool_message``) and the function
    returns ``None`` without contacting Telegram. The runtime prepends the
    buffered messages to the session's "Background done" completion card.
    Set ``suppress_if_backgrounded=False`` (or omit ``action_session_id``)
    to bypass the hook and deliver normally.
    """
    if not token or not response.strip():
        return None
    if (
        action_session_id
        and suppress_if_backgrounded
        and _is_session_backgrounded(action_session_id)
    ):
        _buffer_for_backgrounded(action_session_id, response)
        return None
    chat_id = str(
        meta.get("chat_id")
        or meta.get("notify_chat_id")
        or env_value(instance_dir, "TELEGRAM_CHAT_ID")
        or ""
    )
    if not chat_id:
        return None
    # 4096 chunked sends (audit feature 6): long replies used to be silently
    # truncated (`response[:4096]`) — footer cut first, slice mid-entity.
    chunks = split_for_telegram(response)
    if len(chunks) > 1:
        log(f"telegram.send.chunked parts={len(chunks)} total_chars={len(response)}")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    message_id: str | None = None
    for index, chunk in enumerate(chunks):
        message_id = _send_text_chunk(
            url=url,
            chat_id=chat_id,
            chunk=chunk,
            meta=meta,
            log=log,
            # Native reply threading only on the first chunk; follow-up
            # chunks read as a continuation, not N replies to one message.
            include_reply=index == 0,
        )
    return message_id


def _send_text_chunk(
    *,
    url: str,
    chat_id: str,
    chunk: str,
    meta: dict[str, Any],
    log: LogFn,
    include_reply: bool,
) -> str | None:
    escaped = to_markdown_v2(chunk)
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": escaped,
        "disable_web_page_preview": True,
        "parse_mode": "MarkdownV2",
    }
    if meta.get("message_thread_id"):
        payload["message_thread_id"] = meta["message_thread_id"]
    if include_reply and meta.get("message_id"):
        # `allow_sending_without_reply` keeps the send working if the
        # original was deleted between ingest and response.
        payload["reply_to_message_id"] = meta["message_id"]
        payload["allow_sending_without_reply"] = True
    try:
        data = _post_with_retry(url, payload, timeout=15, log=log)
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
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if meta.get("message_thread_id"):
            fallback["message_thread_id"] = meta["message_thread_id"]
        if include_reply and meta.get("message_id"):
            fallback["reply_to_message_id"] = meta["message_id"]
            fallback["allow_sending_without_reply"] = True
        data = _post_with_retry(url, fallback, timeout=15, log=log)
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
    caption: str = "",
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
    if meta.get("message_id"):
        fields.append(("reply_to_message_id", str(meta["message_id"])))
        fields.append(("allow_sending_without_reply", "true"))
    if caption:
        # Truncate the raw caption, then escape (audit F-P2): slicing the
        # escaped form could cut an escape pair mid-entity → 400 → the
        # whole voice message lost. Shrink until the escaped form fits.
        caption_src = caption[:1024]
        escaped_caption = to_markdown_v2(caption_src)
        while len(escaped_caption) > 1024 and caption_src:
            caption_src = caption_src[: max(0, len(caption_src) - 64)]
            escaped_caption = to_markdown_v2(caption_src)
        fields.append(("caption", escaped_caption))
        fields.append(("parse_mode", "MarkdownV2"))
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


def send_photo(
    *,
    instance_dir: Path,
    token: str,
    image_path: str,
    meta: dict[str, Any],
    caption: str = "",
    log: LogFn | None = None,
) -> str | None:
    """Upload a local image file and post it as a Telegram photo message.

    Returns the message_id on success, None on failure. Failures are logged
    when ``log`` is provided (audit feature 6 — errors used to be swallowed
    and the caller logged "image sent" unconditionally).
    """
    _log = log or (lambda _msg: None)
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
    path = Path(image_path)
    if not path.exists():
        return None
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "image/jpeg"
    fields: list[tuple[str, str]] = [("chat_id", chat_id)]
    if meta.get("message_thread_id"):
        fields.append(("message_thread_id", str(meta["message_thread_id"])))
    if caption:
        fields.append(("caption", caption[:1024]))
    files: list[tuple[str, str, bytes, str]] = [
        ("photo", path.name, path.read_bytes(), mime),
    ]
    body, content_type = encode_multipart(fields, files)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=body,
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        _log(f"telegram sendPhoto transport error path={image_path}: {exc}")
        return None
    try:
        data = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, ValueError):
        _log(f"telegram sendPhoto non-JSON response path={image_path}: {raw[:200]!r}")
        return None
    if not data.get("ok"):
        _log(f"telegram sendPhoto failed path={image_path}: {data}")
        return None
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
