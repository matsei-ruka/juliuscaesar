"""WhatsApp sidecar JSON protocol encode/decode.

The Python channel communicates with the Node sidecar over stdio JSON lines.
This module defines the message types and provides encode/decode helpers.

Sidecar → Python (stdout):
  - qr                QR code ready for scan
  - connection         Socket state changes (open, close, logged_out, etc.)
  - message            Normalized inbound WhatsApp message
  - send_result        Result of an outbound send command
  - error              Fatal or non-fatal error

Python → Sidecar (stdin):
  - send               Send a text message
  - stop               Graceful shutdown
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ── Outgoing (sidecar → Python) ────────────────────────────────────────────

@dataclass(frozen=True)
class QrEvent:
    type: str = "qr"
    qr: str = ""


@dataclass(frozen=True)
class ConnectionEvent:
    type: str = "connection"
    state: str = ""            # open | close | connecting | reconnecting | logged_out | auth_missing
    self_jid: str = ""
    reason: str = ""
    status_code: int = 0
    will_reconnect: bool = False


@dataclass(frozen=True)
class MediaInfo:
    type: str = ""             # image | audio | video | document | sticker
    mime_type: str = ""
    height: int = 0
    width: int = 0
    seconds: int = 0
    file_name: str = ""
    file_size: int = 0


@dataclass(frozen=True)
class WhatsAppMessage:
    type: str = "message"
    message_id: str = ""
    remote_jid: str = ""
    sender_jid: str = ""
    chat_type: str = ""        # dm | group
    from_me: bool = False
    push_name: str = ""
    timestamp: str = ""
    text: str | None = None
    mentions: tuple[str, ...] = ()
    quoted_message_id: str | None = None
    media: dict[str, Any] | None = None
    raw_kind: str = ""         # conversation | extended_text | media
    group_jid: str = ""


@dataclass(frozen=True)
class SendResult:
    type: str = "send_result"
    id: str = ""
    ok: bool = False
    message_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class SidecarError:
    type: str = "error"
    fatal: bool = False
    reason: str = ""


# ── Incoming (Python → sidecar) ────────────────────────────────────────────

@dataclass(frozen=True)
class SendCommand:
    type: str = "send"
    id: str = ""
    to: str = ""
    text: str = ""
    quoted_message_id: str | None = None
    media: None = None


@dataclass(frozen=True)
class DownloadCommand:
    type: str = "download"
    id: str = ""
    message_key: dict[str, Any] = field(default_factory=dict)
    dest_path: str = ""


@dataclass(frozen=True)
class StopCommand:
    type: str = "stop"


@dataclass(frozen=True)
class DownloadResult:
    type: str = "download_result"
    id: str = ""
    ok: bool = False
    dest_path: str = ""
    mime_type: str = ""
    file_size: int = 0
    error: str = ""


# ── Encoding / decoding ─────────────────────────────────────────────────────

def encode_command(cmd: SendCommand | DownloadCommand | StopCommand) -> str:
    """Serialize a command to a JSON line for the sidecar's stdin."""
    if cmd.type == "send":
        data: dict[str, Any] = {"type": "send"}
        data["id"] = cmd.id
        data["to"] = cmd.to
        data["text"] = cmd.text
        if cmd.quoted_message_id:
            data["quoted_message_id"] = cmd.quoted_message_id
        data["media"] = None
        return json.dumps(data, ensure_ascii=False)
    if cmd.type == "download":
        data = {
            "type": "download",
            "id": cmd.id,
            "message_key": cmd.message_key,
            "dest_path": cmd.dest_path,
        }
        return json.dumps(data, ensure_ascii=False)
    # stop
    return json.dumps({"type": "stop"})


def decode_event(line: str) -> dict[str, Any]:
    """Parse a JSON line from the sidecar's stdout.

    Returns the raw dict. Callers should inspect `type` and dispatch.
    Malformed lines return {"type": "_parse_error", "raw": line}.
    """
    stripped = line.strip()
    if not stripped:
        return {"type": "_empty"}
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return {"type": "_parse_error", "raw": stripped, "error": str(exc)}
    if not isinstance(data, dict):
        return {"type": "_parse_error", "raw": stripped, "error": "not a JSON object"}
    return data


def parse_whatsapp_message(raw: dict[str, Any]) -> WhatsAppMessage:
    """Parse a normalized message dict into a WhatsAppMessage."""
    return WhatsAppMessage(
        type="message",
        message_id=str(raw.get("message_id", "")),
        remote_jid=str(raw.get("remote_jid", "")),
        sender_jid=str(raw.get("sender_jid", "")),
        chat_type=str(raw.get("chat_type", "")),
        from_me=bool(raw.get("from_me", False)),
        push_name=str(raw.get("push_name", "")),
        timestamp=str(raw.get("timestamp", "")),
        text=raw.get("text") if raw.get("text") is not None else None,
        mentions=tuple(
            str(m) for m in (raw.get("mentions") or []) if str(m)
        ),
        quoted_message_id=(
            str(raw["quoted_message_id"])
            if raw.get("quoted_message_id") is not None
            else None
        ),
        media=raw.get("media") if isinstance(raw.get("media"), dict) else None,
        raw_kind=str(raw.get("raw_kind", "")),
        group_jid=str(raw.get("group_jid", "")),
    )
