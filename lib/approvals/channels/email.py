"""Email notify + decide adapter (DKIM-verified) for unified approvals."""

from __future__ import annotations

import logging
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .. import dkim as dkim_helper
from ..conf import load_approvals_config
from ..models import Approval
from ..principal import load_principal


logger = logging.getLogger("approvals.channels.email")


DECIDE_LINE_RE = re.compile(
    r"^(APPROVE|REJECT)\s+([0-9a-f]{32})\s+([0-9a-f]{64})\s*$",
    re.IGNORECASE,
)


def notify(instance_dir: Path, record: Approval) -> bool:
    """Send the approval card by email (best-effort)."""
    if not record.notify_email:
        return False
    principal = load_principal(instance_dir)
    if not principal.email:
        logger.info("approvals email notify skipped: no principal email")
        return False

    from gateway.config import env_value

    smtp_host = env_value(instance_dir, "EMAIL_SMTP_HOST")
    if not smtp_host:
        logger.info("approvals email notify skipped: EMAIL_SMTP_HOST unset")
        return False
    smtp_port = int(env_value(instance_dir, "EMAIL_SMTP_PORT") or "587")
    smtp_user = env_value(instance_dir, "EMAIL_SMTP_USER")
    smtp_pass = env_value(instance_dir, "EMAIL_SMTP_PASSWORD")
    sender = env_value(instance_dir, "EMAIL_FROM") or smtp_user or "jc@localhost"

    msg = EmailMessage()
    msg["Subject"] = f"[JC approval] {record.kind}: {record.title}"
    msg["From"] = sender
    msg["To"] = principal.email
    msg.set_content(render_body(record))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as conn:
            conn.ehlo()
            try:
                conn.starttls()
            except smtplib.SMTPException:
                pass
            conn.ehlo()
            if smtp_user and smtp_pass:
                conn.login(smtp_user, smtp_pass)
            conn.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("approvals email send failed: %s", exc)
        return False

    from ..service import mark_notified

    mark_notified(instance_dir, record.approval_id)
    return True


def render_body(record: Approval) -> str:
    """Plain-text email body. Operator replies with APPROVE/REJECT <id> <token>."""
    lines = [
        f"A pending approval needs your decision.",
        "",
        f"  Kind:        {record.kind}",
        f"  Title:       {record.title}",
        f"  Approval id: {record.approval_id}",
        f"  Requested:   {record.requested_at}",
    ]
    if record.expires_at:
        lines.append(f"  Expires:     {record.expires_at}")
    if record.body:
        lines.extend(["", record.body])
    lines.extend(
        [
            "",
            "To approve, reply to this email with the single line:",
            "",
            f"  APPROVE {record.approval_id} {record.callback_token}",
            "",
            "To reject:",
            "",
            f"  REJECT {record.approval_id} {record.callback_token}",
            "",
            "The reply must be DKIM-signed by your domain.",
        ]
    )
    return "\n".join(lines)


def parse_decide_line(text: str) -> tuple[str, str, str] | None:
    """Match the first non-empty line against `APPROVE|REJECT <id> <token>`."""
    if not text:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = DECIDE_LINE_RE.match(line)
        if not m:
            return None
        action_word, approval_id, callback_token = m.group(1), m.group(2), m.group(3)
        action = "approve" if action_word.upper() == "APPROVE" else "reject"
        return action, approval_id.lower(), callback_token.lower()
    return None


def verify_inbound(
    instance_dir: Path,
    *,
    raw_message: bytes,
    parsed_message: Any | None = None,
) -> dict[str, Any]:
    """Verify principal + DKIM + decide-line on an inbound email.

    Returns ``{"ok": True, "approval": Approval, "action": ...}`` or
    ``{"ok": False, "error": "<reason>"}``.
    """
    principal = load_principal(instance_dir)
    if not principal.email:
        return {"ok": False, "error": "principal_email_unset"}

    if parsed_message is None:
        try:
            import email as email_lib

            parsed_message = email_lib.message_from_bytes(raw_message)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"parse_error:{exc.__class__.__name__}"}

    from_header = str(parsed_message.get("From") or "")
    sender_addr = _extract_addr(from_header)
    if sender_addr.lower() != principal.email.lower():
        return {"ok": False, "error": "from_mismatch"}

    cfg = load_approvals_config(instance_dir)
    domain = principal.email_domain or sender_addr.split("@", 1)[-1]

    dkim_ok = dkim_helper.authentication_results_pass(
        parsed_message, cfg.dkim_trusted_mta_hostnames
    )
    if not dkim_ok:
        if not dkim_helper.dkim_available():
            return {"ok": False, "error": "dkim_unavailable"}
        passed, reason = dkim_helper.verify_message(raw_message)
        if not passed:
            return {"ok": False, "error": f"dkim_fail:{reason}"}
        sig_domain = dkim_helper.signing_domain(parsed_message)
        if sig_domain and sig_domain.lower() != domain.lower():
            return {"ok": False, "error": "dkim_domain_mismatch"}

    body_text = _extract_body(parsed_message)
    parsed = parse_decide_line(body_text)
    if not parsed:
        return {"ok": False, "error": "decide_line_missing"}
    action, approval_id, callback_token = parsed

    from ..models import ApprovalConflict, ApprovalNotFound
    from ..service import decide

    try:
        record = decide(
            instance_dir,
            approval_id,
            action=action,
            decided_by=f"email:{sender_addr}",
            decision_channel="email",
            callback_token=callback_token,
        )
    except ApprovalNotFound:
        return {"ok": False, "error": "not_found"}
    except ApprovalConflict as exc:
        return {"ok": False, "error": f"conflict:{exc}"}
    except PermissionError:
        return {"ok": False, "error": "callback_token_mismatch"}
    return {"ok": True, "approval": record, "action": action}


def _extract_addr(raw: str) -> str:
    raw = (raw or "").strip()
    if "<" in raw and ">" in raw:
        return raw[raw.index("<") + 1 : raw.index(">")].strip()
    return raw


def _extract_body(parsed_message: Any) -> str:
    if parsed_message is None:
        return ""
    if parsed_message.is_multipart():
        for part in parsed_message.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_content()
                except Exception:  # noqa: BLE001
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode("utf-8", errors="replace")
        return ""
    try:
        return parsed_message.get_content()
    except Exception:  # noqa: BLE001
        payload = parsed_message.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")
