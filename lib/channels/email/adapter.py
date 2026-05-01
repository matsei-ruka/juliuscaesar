"""Email channel adapter for JuliusCaesar gateway."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from .authorization import SenderAuthorizer
from .imap_client import IMAPClient
from .sanitize import wrap_email_prompt
from .smtp_client import SMTPClient

__all__ = ["EmailChannelAdapter"]


class EmailChannelAdapter:
    """Email channel for JC gateway. IMAP inbound, SMTP outbound."""

    def __init__(
        self,
        instance_dir: Path,
        config: dict[str, Any],
        env: dict[str, str],
    ):
        """
        Args:
            instance_dir: JC instance directory
            config: Email channel config from gateway.yaml
            env: Environment variables (.env)
        """
        self.instance_dir = Path(instance_dir)
        self.config = config
        self.env = env

        # IMAP config
        imap_cfg = config.get("imap", {})
        self.imap_host = imap_cfg.get("host") or env.get("IMAP_HOST")
        self.imap_port = int(imap_cfg.get("port", env.get("IMAP_PORT", 993)))
        self.imap_user = imap_cfg.get("user") or env.get("IMAP_USER")
        self.imap_password = imap_cfg.get("password") or env.get("IMAP_PASSWORD")
        self.imap_mailbox = imap_cfg.get("mailbox", "INBOX")

        # SMTP config
        smtp_cfg = config.get("smtp", {})
        self.smtp_port = int(smtp_cfg.get("port", env.get("SMTP_PORT", 587)))
        self.smtp_sent_folder = smtp_cfg.get("sent_folder", "Sent")
        self.smtp_signature = smtp_cfg.get("signature", "")

        # Limits
        self.body_limit = int(config.get("body_limit", 8000))

        # State
        state_cfg = config.get("state", {})
        self.last_uid_file = Path(
            state_cfg.get("last_uid_file", "state/channels/email/last_uid")
        )
        if not self.last_uid_file.is_absolute():
            self.last_uid_file = self.instance_dir / self.last_uid_file

        # Clients
        self.imap_client = IMAPClient(
            host=self.imap_host,
            user=self.imap_user,
            password=self.imap_password,
            port=self.imap_port,
        )
        self.smtp_client = SMTPClient(
            host=self.imap_host,
            user=self.imap_user,
            password=self.imap_password,
            smtp_port=self.smtp_port,
            sent_folder=self.smtp_sent_folder,
        )

        # Authorization
        gateway_yaml = self.instance_dir / "ops" / "gateway.yaml"
        self.authorizer = SenderAuthorizer(gateway_yaml)

    def fetch_new_messages(self) -> list[dict[str, Any]]:
        """Fetch new messages since last UID. Returns messages ready for dispatch."""
        # Load watermark
        last_uid = self._load_last_uid()

        # Fetch from IMAP
        try:
            self.imap_client.connect()
            email_messages = self.imap_client.fetch_new(last_uid, self.imap_mailbox)
        finally:
            self.imap_client.disconnect()

        if not email_messages:
            return []

        # Filter by authorization + wrap as prompts
        dispatched = []
        for msg in email_messages:
            status = self.authorizer.check(msg.sender)

            prompt_text = wrap_email_prompt(
                sender=msg.sender,
                subject=msg.subject,
                body=msg.body,
                is_html=False,  # Already extracted text by IMAPClient
                max_chars=self.body_limit,
            )

            dispatch = {
                "channel": "email",
                "channel_id": f"uid_{msg.uid}",
                "conversation_id": f"email_{msg.sender.lower()}",
                "user_id": f"email_{msg.sender.lower()}",
                "sender": msg.sender,
                "sender_name": msg.sender_name,
                "subject": msg.subject,
                "message_id": msg.message_id,
                "in_reply_to": msg.in_reply_to,
                "references": msg.references,
                "text": prompt_text,
                "status": status,  # 'trusted', 'external', 'blocked', 'unknown'
                "metadata": {
                    "uid": msg.uid,
                    "date": msg.date,
                    "is_unread": msg.is_unread,
                },
            }

            dispatched.append(dispatch)

        return dispatched

    def mark_handled_uids(self, uids: list[str]) -> None:
        """Advance the UID watermark after local durable handling succeeds."""
        current = self._load_last_uid()
        highest = current
        for uid in uids:
            try:
                uid_int = int(uid)
            except (TypeError, ValueError):
                continue
            highest = max(highest, uid_int)
        if highest > current:
            self._save_last_uid(highest)

    def send_reply(
        self,
        conversation_id: str,
        recipient: str,
        subject: str,
        body: str,
        in_reply_to: Optional[str] = None,
        references: Optional[list[str]] = None,
    ) -> str:
        """Send reply via SMTP. Returns Message-ID."""
        references_str = " ".join(references) if references else None

        message_id = self.smtp_client.send(
            to=[recipient],
            cc=[],
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
            references=references_str,
            signature=self.smtp_signature,
        )
        return message_id

    def _load_last_uid(self) -> int:
        """Load UID watermark from state file."""
        if not self.last_uid_file.exists():
            return 0
        try:
            return int(self.last_uid_file.read_text().strip() or "0")
        except ValueError:
            return 0

    def _save_last_uid(self, uid: int) -> None:
        """Save UID watermark to state file."""
        self.last_uid_file.parent.mkdir(parents=True, exist_ok=True)
        self.last_uid_file.write_text(str(uid))
