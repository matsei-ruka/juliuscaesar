"""Email dispatch logic — shared between EmailChannel poller and the
`heartbeat/fetch/email-poll.sh` external cron script.

Routes EmailChannelAdapter outputs:
  - status='trusted'  → enqueue to gateway queue (source='email')
  - status='external' → enqueue to gateway queue (outbound reply drafts)
  - status='unknown'  → notify Telegram + persist message JSON to
                        `state/channels/email/pending/<uid>.json`
  - status='blocked'  → silent drop

Pending messages are drained when an operator runs
`jc-chats approve --email <addr>` or `... deny --email <addr>` —
that path is implemented in `bin/jc-chats`, which calls `drain_pending`.

Also exposes a `poll` CLI entrypoint:
    python3 -m gateway.channels.email_dispatcher poll --instance-dir <path>
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml


__all__ = [
    "DispatchResult",
    "dispatch_messages",
    "drain_pending",
    "drafts_dir",
    "enqueue_draft",
    "pending_dir",
    "main",
]


PENDING_REL = Path("state/channels/email/pending")
DRAFTS_REL = Path("state/channels/email/drafts")


@dataclass
class DispatchResult:
    dispatched: int = 0
    pending: int = 0
    blocked: int = 0
    handled_uids: list[str] = field(default_factory=list)


def _sender_key(sender: str) -> str:
    """Return a filesystem-safe stable key for a normalized email address."""
    normalized = (sender or "unknown").lower().strip()
    token = base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii")
    return token.rstrip("=") or "unknown"


def _message_uid(msg: dict[str, Any]) -> str:
    return str(msg.get("metadata", {}).get("uid") or msg.get("channel_id") or "0")


def pending_dir(instance_dir: Path) -> Path:
    """Where unknown-sender messages are persisted while awaiting approval."""
    return instance_dir / PENDING_REL


def drafts_dir(instance_dir: Path) -> Path:
    """Where external-sender outbound replies wait for approval."""
    return instance_dir / DRAFTS_REL


def _write_pending(instance_dir: Path, msg: dict[str, Any]) -> Path:
    """Persist `msg` under `<sender>/<uid>.json` so drain_pending can find it."""
    sender = (msg.get("sender") or "unknown").lower().strip()
    uid = _message_uid(msg)
    path = pending_dir(instance_dir) / _sender_key(sender) / f"{uid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(msg, default=str), encoding="utf-8")
    return path


def _meta_for_event(msg: dict[str, Any]) -> dict[str, Any]:
    """Build the meta dict that travels with the gateway event.

    Outbound delivery (`EmailChannel.send`) reads `email_to`, `email_subject`,
    `email_message_id`, `email_references` from this dict to build the reply.
    """
    return {
        "delivery_channel": "email",
        "email_to": msg.get("sender"),
        "email_to_name": msg.get("sender_name"),
        "email_subject": msg.get("subject"),
        "email_message_id": msg.get("message_id"),
        "email_in_reply_to": msg.get("in_reply_to"),
        "email_references": msg.get("references") or [],
        "email_uid": str(msg.get("metadata", {}).get("uid", "")),
        "channel_id": msg.get("channel_id"),
        "sender_tier": "trusted" if msg.get("status") == "allowed" else msg.get("status"),
    }


def _enqueue_message(
    *,
    instance_dir: Path,
    msg: dict[str, Any],
    enqueue: Optional[Callable[..., None]] = None,
) -> None:
    """Push a message into the gateway queue, either via injected callable
    (used when called from inside a live EmailChannel.run) or by opening
    a fresh sqlite connection (used by the heartbeat poll script)."""
    meta = _meta_for_event(msg)
    if enqueue is not None:
        enqueue(
            source="email",
            source_message_id=msg.get("channel_id"),
            user_id=msg.get("user_id"),
            conversation_id=msg.get("conversation_id"),
            content=msg.get("text", ""),
            meta=meta,
        )
        return
    # External-poll path: use the queue module directly.
    from .. import queue as queue_module
    conn = queue_module.connect(instance_dir)
    try:
        queue_module.enqueue(
            conn,
            source="email",
            source_message_id=msg.get("channel_id"),
            user_id=msg.get("user_id"),
            conversation_id=msg.get("conversation_id"),
            content=msg.get("text", ""),
            meta=meta,
        )
    finally:
        conn.close()


def _send_telegram_notify(
    instance_dir: Path,
    body: str,
    chat_id_override: Optional[str] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Best-effort Telegram notification reusing the heartbeat sender."""
    repo_root = Path(__file__).resolve().parents[3]
    sender = repo_root / "lib" / "heartbeat" / "lib" / "send_telegram.py"
    if not sender.exists():
        if log:
            log(f"telegram notify skipped — sender not found at {sender}")
        return None
    env = os.environ.copy()
    env["JC_INSTANCE_DIR"] = str(instance_dir)
    if chat_id_override:
        env["TELEGRAM_CHAT_ID_OVERRIDE"] = str(chat_id_override)
    try:
        proc = subprocess.run(
            [sys.executable, str(sender)],
            input=body,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"telegram notify failed: {exc}")
        return None
    if proc.returncode != 0:
        if log:
            log(f"telegram notify rc={proc.returncode} stderr={proc.stderr[:200]}")
        return None
    return proc.stdout.strip() or None


def _format_unknown_notification(msg: dict[str, Any]) -> str:
    sender = msg.get("sender") or "(unknown)"
    name = msg.get("sender_name") or sender
    subject = msg.get("subject") or "(no subject)"
    preview = (msg.get("text") or "")[:200].replace("\n", " ").strip()
    if len(msg.get("text") or "") > 200:
        preview += "…"
    return (
        f"📧 New email from unknown sender\n\n"
        f"**From:** {name} `{sender}`\n"
        f"**Subject:** {subject}\n\n"
        f"_{preview}_\n\n"
        f"Approve: `jc-chats approve --email {sender}`\n"
        f"Deny: `jc-chats deny --email {sender}`"
    )


def _approval_cfg(cfg: dict[str, Any]) -> tuple[bool, str | None, bool]:
    approvals = cfg.get("approvals") if isinstance(cfg.get("approvals"), dict) else {}
    notify_on_unknown = bool(
        approvals.get("notify_on_unknown", cfg.get("notify_on_unknown", True))
    )
    notify_on_draft = bool(approvals.get("notify_on_draft", True))
    notify_chat_id = approvals.get("telegram_chat_id", cfg.get("telegram_chat_id"))
    notify_chat_id = str(notify_chat_id) if notify_chat_id else None
    return notify_on_unknown, notify_chat_id, notify_on_draft


def _format_draft_notification(draft: dict[str, Any]) -> str:
    draft_id = draft["draft_id"]
    sender = draft["sender"]
    subject = draft["subject"]
    preview = (draft["draft_text"] or "")[:500].replace("\n", " ").strip()
    if len(draft.get("draft_text") or "") > 500:
        preview += "..."
    return (
        "Email draft pending approval\n\n"
        f"From: `{sender}`\n"
        f"Subject: {subject}\n"
        f"Draft: `{draft_id}`\n\n"
        f"{preview}\n\n"
        f"Approve: `jc email drafts approve {draft_id}`\n"
        f"Reject: `jc email drafts reject {draft_id}`\n"
        f"Edit: `jc email drafts edit {draft_id} <text>`"
    )


def enqueue_draft(
    instance_dir: Path,
    *,
    response: str,
    meta: dict[str, Any],
    cfg: Optional[dict[str, Any]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> str:
    """Persist an external-sender outbound reply as a draft and notify Telegram."""
    if log is None:
        log = lambda _msg: None  # noqa: E731
    cfg = cfg or {}
    sender = str(meta.get("email_to") or meta.get("recipient") or meta.get("sender") or "unknown")
    uid = str(meta.get("email_uid") or meta.get("channel_id") or "manual")
    draft_id = f"draft_{uid}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    draft = {
        "draft_id": draft_id,
        "sender": sender,
        "sender_key": _sender_key(sender),
        "subject": meta.get("email_subject") or meta.get("subject") or "(no subject)",
        "message_id": meta.get("email_message_id") or meta.get("message_id"),
        "in_reply_to": meta.get("email_message_id") or meta.get("in_reply_to"),
        "references": meta.get("email_references") or meta.get("references") or [],
        "draft_text": response,
        "draft_timestamp": now,
        "edit_count": 0,
        "state": "pending",
        "meta": meta,
    }
    path = drafts_dir(instance_dir) / draft["sender_key"] / f"{draft_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft, indent=2, default=str), encoding="utf-8")
    log(f"email draft queued draft_id={draft_id} sender={sender} path={path}")

    _notify_on_unknown, notify_chat_id, notify_on_draft = _approval_cfg(cfg)
    if notify_on_draft:
        _send_telegram_notify(
            instance_dir,
            _format_draft_notification(draft),
            chat_id_override=notify_chat_id,
            log=log,
        )
    return draft_id


def dispatch_messages(
    *,
    instance_dir: Path,
    messages: Iterable[dict[str, Any]],
    enqueue: Optional[Callable[..., None]] = None,
    cfg: Optional[dict[str, Any]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> DispatchResult:
    """Route `messages` according to their `status` field.

    `cfg` is the raw `channels.email` block from gateway.yaml. Used for
    `notify_on_unknown` and `telegram_chat_id`.
    """
    if log is None:
        log = lambda _msg: None  # noqa: E731
    cfg = cfg or {}
    notify_on_unknown, notify_chat_id, _notify_on_draft = _approval_cfg(cfg)

    result = DispatchResult()
    for msg in messages:
        status = msg.get("status", "unknown")
        sender = msg.get("sender", "(unknown)")
        uid = _message_uid(msg)
        if status in {"allowed", "trusted", "external"}:
            try:
                _enqueue_message(instance_dir=instance_dir, msg=msg, enqueue=enqueue)
                result.dispatched += 1
                result.handled_uids.append(uid)
                log(f"email dispatched uid={msg.get('channel_id')} sender={sender}")
            except Exception as exc:  # noqa: BLE001
                log(f"email enqueue failed sender={sender}: {exc}")
        elif status == "blocked":
            result.blocked += 1
            result.handled_uids.append(uid)
            log(f"email dropped (blocked) sender={sender}")
        else:  # unknown
            try:
                path = _write_pending(instance_dir, msg)
                result.pending += 1
                result.handled_uids.append(uid)
                log(f"email pending sender={sender} path={path}")
            except OSError as exc:
                log(f"email pending write failed sender={sender}: {exc}")
                continue
            if notify_on_unknown:
                _send_telegram_notify(
                    instance_dir,
                    _format_unknown_notification(msg),
                    chat_id_override=notify_chat_id,
                    log=log,
                )
    return result


def drain_pending(
    instance_dir: Path,
    sender: str,
    *,
    action: str,
    log: Optional[Callable[[str], None]] = None,
) -> int:
    """Process all pending messages for `sender`.

    `action='approve'` → enqueue each; `action='deny'` → discard.
    Returns count of messages handled. Sender folder is removed afterwards.
    """
    if log is None:
        log = lambda _msg: None  # noqa: E731
    sender_norm = sender.lower().strip()
    sender_dir = pending_dir(instance_dir) / _sender_key(sender_norm)
    if not sender_dir.is_dir():
        return 0
    count = 0
    for path in sorted(sender_dir.glob("*.json")):
        try:
            msg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"pending parse failed {path}: {exc}")
            path.unlink(missing_ok=True)
            continue
        if action == "approve":
            try:
                msg["status"] = "trusted"
                _enqueue_message(instance_dir=instance_dir, msg=msg, enqueue=None)
                log(f"pending dispatched uid={msg.get('channel_id')} sender={sender}")
            except Exception as exc:  # noqa: BLE001
                log(f"pending enqueue failed {path}: {exc}")
                continue
        else:
            log(f"pending dropped uid={msg.get('channel_id')} sender={sender}")
        path.unlink(missing_ok=True)
        count += 1
    try:
        sender_dir.rmdir()
    except OSError:
        pass
    return count


def _load_yaml_email_cfg(instance_dir: Path) -> dict[str, Any]:
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
    return channels.get("email") or {}


def _cli_poll(args: argparse.Namespace) -> int:
    """One-shot poll: fetch via adapter, dispatch, exit. Used by cron."""
    from dotenv import load_dotenv  # type: ignore

    instance = Path(args.instance_dir).resolve()
    env_file = instance / ".env"
    if env_file.exists():
        load_dotenv(str(env_file))

    cfg_raw = _load_yaml_email_cfg(instance)
    if not cfg_raw.get("enabled", True):
        # Channel block exists but is disabled — nothing to do.
        print("email channel disabled in gateway.yaml; skipping")
        return 0

    env = {
        "IMAP_HOST": os.environ.get("IMAP_HOST", ""),
        "IMAP_PORT": os.environ.get("IMAP_PORT", ""),
        "IMAP_USER": os.environ.get("IMAP_USER", ""),
        "IMAP_PASSWORD": os.environ.get("IMAP_PASSWORD", ""),
        "SMTP_PORT": os.environ.get("SMTP_PORT", ""),
    }

    from channels.email import EmailChannelAdapter  # type: ignore

    adapter = EmailChannelAdapter(
        instance_dir=instance,
        config=cfg_raw,
        env=env,
    )
    log_path = instance / "state" / "channels" / "email" / "poll.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(line: str) -> None:
        from datetime import datetime
        ts = datetime.now().isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")
        if args.verbose:
            print(line)

    try:
        messages = adapter.fetch_new_messages()
    except Exception as exc:  # noqa: BLE001
        log(f"poll error: fetch_new_messages: {exc}")
        return 1
    log(f"poll: fetched={len(messages)}")
    result = dispatch_messages(
        instance_dir=instance,
        messages=messages,
        enqueue=None,
        cfg=cfg_raw,
        log=log,
    )
    log(
        f"poll done: dispatched={result.dispatched} "
        f"pending={result.pending} blocked={result.blocked}"
    )
    if hasattr(adapter, "mark_handled_uids"):
        adapter.mark_handled_uids(result.handled_uids)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="email_dispatcher")
    sub = parser.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("poll", help="one-shot IMAP poll + dispatch")
    pp.add_argument("--instance-dir", required=True)
    pp.add_argument("-v", "--verbose", action="store_true")
    pp.set_defaults(func=_cli_poll)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
