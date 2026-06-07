"""WhatsApp channel — gateway integration.

Manages the Node.js sidecar subprocess, applies Trusted/External/Blocked
access control, enqueues inbound messages into the gateway queue, and
handles outbound delivery with draft support for External senders.

Matches the EmailChannel pattern in ``email.py``.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from ..config import ChannelConfig, env_value
from .base import EnqueueFn, LogFn
from . import whatsapp_policy as policy
from . import whatsapp_state as state
from . import whatsapp_protocol as protocol
from .whatsapp_sidecar import WhatsAppSidecar, SidecarError


def _load_wa_config(instance_dir: Path) -> dict[str, Any]:
    """Read the raw ``channels.whatsapp`` block from ``ops/gateway.yaml``."""
    path = instance_dir / "ops" / "gateway.yaml"
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    channels = data.get("channels") or {}
    wa_cfg = channels.get("whatsapp") or {}
    return wa_cfg if isinstance(wa_cfg, dict) else {}


def _auth_dir(instance_dir: Path, account_id: str) -> Path:
    return instance_dir / "state" / "channels" / "whatsapp" / "auth" / account_id


def _is_mention(mentions: tuple[str, ...], self_jid: str) -> bool:
    """Check if self_jid appears in mentions (case-insensitive JID prefix)."""
    self_norm = self_jid.split("@")[0].lower() if "@" in self_jid else self_jid.lower()
    for m in mentions:
        m_norm = m.split("@")[0].lower() if "@" in m else m.lower()
        if m_norm == self_norm:
            return True
    return False


class WhatsAppChannel:
    name = "whatsapp"

    def __init__(self, instance_dir: Path, cfg: ChannelConfig, log: LogFn):
        self.instance_dir = instance_dir
        self.cfg = cfg
        self.log = log
        self._sidecar: WhatsAppSidecar | None = None
        self._self_jid: str = ""
        self._connection_open = threading.Event()
        self._fatal_error: str | None = None

    # ── Channel lifecycle ───────────────────────────────────────────────

    def ready(self) -> bool:
        """Return True if the channel is configured and can start."""
        cfg_raw = _load_wa_config(self.instance_dir)
        accounts = cfg_raw.get("accounts") if isinstance(cfg_raw.get("accounts"), dict) else {}
        if not accounts:
            self.log("whatsapp channel disabled — no accounts configured")
            return False
        # Check if node is available
        import shutil
        if not shutil.which("node"):
            self.log("whatsapp channel disabled — node not found in PATH")
            return False
        return True

    def run(self, enqueue: EnqueueFn, should_stop: Callable[[], bool]) -> None:
        """Start the WhatsApp channel and block until stopped."""
        if not self.ready():
            return

        cfg_raw = _load_wa_config(self.instance_dir)
        accounts = cfg_raw.get("accounts") if isinstance(cfg_raw.get("accounts"), dict) else {}

        account_id = "default"  # Start with default; multi-account loops later

        auth = str(_auth_dir(self.instance_dir, account_id))
        self._sidecar = WhatsAppSidecar(
            auth_dir=auth,
            account_id=account_id,
            on_event=lambda ev: self._handle_sidecar_event(ev, enqueue),
            on_log=self.log,
        )

        try:
            self._sidecar.start()
        except SidecarError as exc:
            self.log(f"whatsapp channel failed to start: {exc}")
            self._fatal_error = str(exc)
            return

        self.log(f"whatsapp channel started account={account_id}")

        # Block until stopped
        while not should_stop():
            time.sleep(1)
            if self._fatal_error:
                self.log(f"whatsapp channel fatal: {self._fatal_error}")
                break

        self._sidecar.stop()
        self.log("whatsapp channel stopped")

    def send(self, response: str, meta: dict[str, Any]) -> str | None:
        """Send or draft an outbound reply.

        Requires meta keys set during inbound enqueue:
          - meta.recipient_jid  (the remote JID to reply to)
          - meta.account_id     (which account)
          - meta.sender_tier    (trusted/external/blocked)
          - meta.quoted_message_id (optional, for reply quoting)

        Trusted → send immediately.
        External → draft for operator approval.
        Blocked → silently skip.
        """
        if not response.strip():
            return None

        recipient = meta.get("recipient_jid") or meta.get("sender_jid") or ""
        if not recipient:
            self.log("whatsapp send skipped — no recipient JID in meta")
            return None

        tier = str(meta.get("sender_tier", "external")).strip().lower()
        account_id = str(meta.get("account_id", "default"))

        if tier == "blocked":
            self.log(f"whatsapp send skipped — sender is blocked jid={recipient}")
            state.record_event(
                self.instance_dir,
                event="send_blocked",
                jid=recipient,
                account_id=account_id,
            )
            return None

        if tier == "external":
            draft_id = f"draft_{int(time.monotonic() * 1000)}"
            state.write_draft(
                self.instance_dir,
                draft_id=draft_id,
                response=response,
                meta=dict(meta),
            )
            state.record_event(
                self.instance_dir,
                event="draft_created",
                draft_id=draft_id,
                jid=recipient,
                account_id=account_id,
            )
            # Notify operator
            self._notify_operator_external(recipient, response, draft_id, meta)
            return f"draft:{draft_id}"

        # Trusted — send immediately
        if self._sidecar is None:
            return None
        try:
            quoted = meta.get("quoted_message_id")
            cmd_id = self._sidecar.send(
                to=recipient,
                text=response,
                quoted_message_id=quoted if isinstance(quoted, str) and quoted else None,
            )
            state.record_event(
                self.instance_dir,
                event="send_queued",
                send_id=cmd_id,
                jid=recipient,
                account_id=account_id,
            )
            return cmd_id
        except SidecarError as exc:
            self.log(f"whatsapp send failed: {exc}")
            return None

    # ── Sidecar event handling ──────────────────────────────────────────

    def _handle_sidecar_event(self, raw: dict[str, Any], enqueue: EnqueueFn) -> None:
        """Dispatch a sidecar event to the appropriate handler."""
        event_type = str(raw.get("type", ""))

        if event_type == "qr":
            self.log(f"QR received (len={len(raw.get('qr', ''))})")
            self._on_qr(raw)

        elif event_type == "connection":
            self._on_connection(raw)

        elif event_type == "message":
            self._on_message(raw, enqueue)

        elif event_type == "send_result":
            self._on_send_result(raw)

        elif event_type == "download_result":
            self._on_download_result(raw)

        elif event_type == "error":
            self.log(f"sidecar error: {raw.get('reason')}")
            if raw.get("fatal"):
                self._fatal_error = str(raw.get("reason", "unknown sidecar error"))

    def _on_qr(self, raw: dict[str, Any]) -> None:
        """QR code received — log it for operator action."""
        qr_str = str(raw.get("qr", ""))
        self.log(
            f"whatsapp QR received. Run 'jc whatsapp login' or scan from terminal."
        )
        state.record_event(self.instance_dir, event="qr_received", qr_len=len(qr_str))

    def _on_connection(self, raw: dict[str, Any]) -> None:
        """Connection state change."""
        conn_state = str(raw.get("state", ""))
        self_jid = str(raw.get("self_jid", ""))
        reason = str(raw.get("reason", ""))

        if conn_state == "open":
            self._self_jid = self_jid
            self._connection_open.set()
            self.log(f"whatsapp connected as {self_jid}")
            state.record_event(
                self.instance_dir, event="connection_open", self_jid=self_jid,
            )

        elif conn_state == "close":
            self._connection_open.clear()
            self.log(f"whatsapp disconnected: {reason}")

        elif conn_state == "logged_out":
            self._connection_open.clear()
            self._fatal_error = f"logged_out: {reason}"
            self.log(f"whatsapp logged out: {reason}. Run 'jc whatsapp login'.")
            state.record_event(
                self.instance_dir, event="logged_out", reason=reason,
            )

        elif conn_state == "auth_missing":
            self._connection_open.clear()
            self._fatal_error = "auth_missing"
            self.log("whatsapp auth missing. Run 'jc whatsapp login'.")
            state.record_event(self.instance_dir, event="auth_missing")

    def _on_message(self, raw: dict[str, Any], enqueue: EnqueueFn) -> None:
        """Inbound WhatsApp message — apply policy and enqueue."""
        msg = protocol.parse_whatsapp_message(raw)
        pol = policy.read_policy(self.instance_dir)
        account_id = str(raw.get("account_id", "default"))

        # Update chat record
        sender_tier = policy.resolve_tier(pol, msg.sender_jid)
        chat_type = msg.chat_type

        state.upsert_chat(self.instance_dir, state.ChatRecord(
            jid=msg.sender_jid if chat_type == "dm" else (msg.group_jid or msg.remote_jid),
            push_name=msg.push_name,
            chat_type=chat_type,
            tier=sender_tier,
            last_message_at=msg.timestamp,
            account_id=account_id,
        ))

        # --- Group messages: extra gates ---
        if chat_type == "group":
            group_jid = msg.group_jid or msg.remote_jid
            group_tier = policy.resolve_group_tier(pol, group_jid)

            if group_tier == "blocked":
                self.log(f"whatsapp group message dropped (group blocked): {group_jid}")
                state.record_event(
                    self.instance_dir, event="group_blocked",
                    group_jid=group_jid, sender_jid=msg.sender_jid,
                )
                return

            if group_tier == "external" and sender_tier != "blocked":
                # External groups: enqueue but draft response
                sender_tier = "external"

            # Mention gate
            if not policy.group_mention_allowed(pol, msg.mentions, self._self_jid):
                self.log(
                    f"whatsapp group message dropped (no mention): "
                    f"group={group_jid} sender={msg.sender_jid}"
                )
                state.record_event(
                    self.instance_dir, event="group_no_mention",
                    group_jid=group_jid, sender_jid=msg.sender_jid,
                )
                return

        # --- Tier gating ---
        if sender_tier == "blocked":
            self.log(f"whatsapp message dropped (blocked): {msg.sender_jid}")
            state.record_event(
                self.instance_dir, event="message_blocked",
                sender_jid=msg.sender_jid, account_id=account_id,
            )
            return

        # --- Enqueue ---
        conversation_id = f"whatsapp:{account_id}:{msg.sender_jid}"
        content = msg.text or ""
        if msg.media:
            media_type = msg.media.get("type", "media")
            content = content or f"[{media_type}]"

        meta: dict[str, Any] = {
            "delivery_channel": "whatsapp",
            "account_id": account_id,
            "sender_jid": msg.sender_jid,
            "recipient_jid": msg.remote_jid,
            "chat_id": msg.sender_jid,
            "chat_type": msg.chat_type,
            "push_name": msg.push_name,
            "sender_tier": sender_tier,
            "quoted_message_id": msg.quoted_message_id,
            "original_text": msg.text or "[media]",
        }
        if msg.media:
            meta["media"] = msg.media

        # Download media synchronously before enqueue so vision routing works.
        media_path: str | None = None
        if msg.media and self._sidecar:
            cfg_raw = _load_wa_config(self.instance_dir)
            accounts = cfg_raw.get("accounts") if isinstance(cfg_raw.get("accounts"), dict) else {}
            acct = accounts.get(account_id, {})
            media_cfg = acct.get("media") if isinstance(acct.get("media"), dict) else {}
            if media_cfg.get("enabled", True):
                max_bytes = int(media_cfg.get("max_bytes", 25_000_000))
                media_dir = (
                    self.instance_dir / "state" / "channels" / "whatsapp"
                    / "media" / account_id / msg.message_id
                )
                ext = _media_ext(msg.media)
                dest = media_dir / f"media{ext}"
                # Write the expected path into meta so the brain can reference it
                # even if the download is still in-flight.
                meta["image_path"] = str(dest)
                media_path = str(dest)

        source_message_id = f"{account_id}:{msg.remote_jid}:{msg.message_id}"

        enqueue(
            source="whatsapp",
            source_message_id=source_message_id,
            user_id=msg.sender_jid,
            conversation_id=conversation_id,
            content=content,
            meta=meta,
        )

        state.record_event(
            self.instance_dir,
            event="message_enqueued",
            tier=sender_tier,
            sender_jid=msg.sender_jid,
            account_id=account_id,
        )

        # Initiate async media download after enqueue.
        # The meta already carries image_path; brain references it.
        # Download result is handled by _on_download_result later.
        if media_path and self._sidecar:
            message_key = {
                "id": msg.message_id,
                "remoteJid": msg.remote_jid,
                "fromMe": False,
            }
            try:
                self._sidecar.download(message_key, media_path)
            except SidecarError as exc:
                self.log(f"media download queued failed: {exc}")

    def _on_send_result(self, raw: dict[str, Any]) -> None:
        """Outbound send result from sidecar."""
        ok = bool(raw.get("ok", False))
        send_id = str(raw.get("id", ""))
        error = str(raw.get("error", ""))
        message_id = str(raw.get("message_id", ""))

        if ok:
            self.log(f"whatsapp send ok id={send_id} wa_msg_id={message_id}")
            state.record_event(
                self.instance_dir, event="send_ok",
                send_id=send_id, wa_msg_id=message_id,
            )
        else:
            self.log(f"whatsapp send failed id={send_id}: {error}")
            state.record_event(
                self.instance_dir, event="send_failed",
                send_id=send_id, error=error,
            )

    # ── Operator notification ───────────────────────────────────────────

    def _notify_operator_external(
        self,
        jid: str,
        response: str,
        draft_id: str,
        meta: dict[str, Any],
    ) -> None:
        """Notify the operator about an External sender requiring approval.

        Tries Telegram first (if configured), otherwise logs prominently.
        """
        push_name = str(meta.get("push_name", jid))
        body = (
            f"📱 *WhatsApp — External sender*\n\n"
            f"*From:* {push_name} (`{jid}`)\n"
            f"*Message:* {meta.get('original_text', '(media)')}\n\n"
            f"*Proposed reply:*\n{response[:500]}"
            f"{'…' if len(response) > 500 else ''}\n\n"
            f"Approve: `jc whatsapp chats trust {jid}`\n"
            f"Block: `jc whatsapp chats block {jid}`\n"
            f"Draft: `{draft_id}`"
        )

        # Try sending via Telegram using the heartbeat sender
        try:
            repo_root = Path(__file__).resolve().parents[3]
            sender = repo_root / "lib" / "heartbeat" / "lib" / "send_telegram.py"
            if sender.exists():
                subprocess.run(
                    ["python3", str(sender), body],
                    capture_output=True,
                    timeout=10,
                )
                self.log(f"operator notified about external sender: {jid}")
        except Exception as exc:
            self.log(f"operator notification failed: {exc}")

    # ── Media handling ─────────────────────────────────────────────────

    def _on_download_result(self, raw: dict[str, Any]) -> None:
        """Handle media download result from sidecar."""
        ok = bool(raw.get("ok", False))
        dl_id = str(raw.get("id", ""))
        dest_path = str(raw.get("dest_path", ""))
        file_size = int(raw.get("file_size", 0))
        error = str(raw.get("error", ""))

        if ok and dest_path:
            self.log(f"media downloaded: {dest_path} ({file_size} bytes)")
            state.record_event(
                self.instance_dir, event="media_downloaded",
                download_id=dl_id, dest_path=dest_path, file_size=file_size,
            )
        else:
            self.log(f"media download failed id={dl_id}: {error}")
            state.record_event(
                self.instance_dir, event="media_download_failed",
                download_id=dl_id, error=error,
            )

    # ── Health (watchdog integration) ──────────────────────────────────

    def _auth_valid(self) -> bool:
        """Return True if the auth state is not known to be invalid.

        _fatal_error can be:
          - None → auth is fine
          - "auth_missing" → no creds exist
          - "logged_out: <reason>" → WhatsApp explicitly logged out
          - <SidecarError message> → unknown, assume valid (not auth-related)
        """
        if not self._fatal_error:
            return True
        if self._fatal_error == "auth_missing":
            return False
        if self._fatal_error.startswith("logged_out"):
            return False
        # Unknown error — don't assume auth is bad
        return True

    def health(self) -> dict[str, Any]:
        """Return structured health status for watchdog consumption.

        The watchdog calls this periodically. Returns a dict with:
          - connected: bool
          - auth_valid: bool
          - fatal_error: str or None
          - self_jid: str
          - account_id: str
          - recent_events: list of recent event dicts
        """
        return {
            "connected": self._connection_open.is_set(),
            "auth_valid": self._auth_valid(),
            "fatal_error": self._fatal_error,
            "self_jid": self._self_jid,
            "account_id": "default",
            "recent_events": state.recent_events(self.instance_dir, limit=10),
        }


def _media_ext(media: dict[str, Any]) -> str:
    """Return a file extension for a media type."""
    mime = str(media.get("mime_type", "")).lower()
    if "jpeg" in mime or "jpg" in mime:
        return ".jpg"
    if "png" in mime:
        return ".png"
    if "webp" in mime:
        return ".webp"
    if "ogg" in mime or "opus" in mime:
        return ".ogg"
    if "mp4" in mime:
        return ".mp4"
    if "pdf" in mime:
        return ".pdf"
    return ".bin"
