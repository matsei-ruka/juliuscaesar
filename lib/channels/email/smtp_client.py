"""SMTP client for email channel. Sends messages and archives to IMAP Sent folder."""

from __future__ import annotations

import email.utils
import imaplib
import smtplib
import ssl
import time
from email.message import EmailMessage as EmailMIME
from typing import Optional

__all__ = ["SMTPClient"]


class SMTPClient:
    """SMTP wrapper with STARTTLS and automatic Sent folder archival."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        smtp_port: int = 587,
        imap_port: int = 993,
        sent_folder: str = "Sent",
        timeout: int = 30,
    ):
        self.host = host
        self.user = user
        self.password = password
        self.smtp_port = smtp_port
        self.imap_port = imap_port
        self.sent_folder = sent_folder
        self.timeout = timeout

    def send(
        self,
        to: list[str],
        cc: list[str],
        subject: str,
        body: str,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
        signature: str = "",
    ) -> str:
        """Send email via SMTP. Returns Message-ID."""
        # Build message
        msg = self._build_message(
            to=to,
            cc=cc,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
            references=references,
            signature=signature,
        )

        # Send via SMTP
        all_recipients = list(to) + list(cc)
        try:
            s = smtplib.SMTP(self.host, self.smtp_port, timeout=self.timeout)
            s.ehlo()
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
            s.login(self.user, self.password)
            refused = s.send_message(msg, from_addr=self.user, to_addrs=all_recipients)
            s.quit()
        except Exception as e:
            raise RuntimeError(f"SMTP send failed: {type(e).__name__}: {e}") from e

        if refused:
            raise RuntimeError(f"Some recipients refused: {refused}")

        message_id = msg["Message-ID"]

        # Archive to Sent (idempotent)
        try:
            self.archive_to_sent(msg)
        except Exception as e:
            # Log but don't fail
            raise RuntimeError(f"Archive to {self.sent_folder} failed: {e}") from e

        return message_id

    def _build_message(
        self,
        to: list[str],
        cc: list[str],
        subject: str,
        body: str,
        in_reply_to: Optional[str],
        references: Optional[str],
        signature: str,
    ) -> EmailMIME:
        """Build RFC 2822 compliant message."""
        msg = EmailMIME()
        msg["From"] = self.user
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = email.utils.make_msgid(
            domain=self.user.split("@", 1)[1]
        )

        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references

        # Body + signature
        full_body = body.rstrip()
        if signature:
            full_body += f"\n\n{signature}"

        msg.set_content(full_body, subtype="plain", charset="utf-8")
        return msg

    def archive_to_sent(self, msg: EmailMIME) -> None:
        """Append message to IMAP Sent folder (creates folder if missing)."""
        ctx = ssl.create_default_context()
        M = imaplib.IMAP4_SSL(self.host, self.imap_port, ssl_context=ctx, timeout=self.timeout)
        try:
            M.login(self.user, self.password)

            # Create folder if missing (ignore "already exists" errors)
            try:
                M.create(self.sent_folder)
            except Exception:
                pass

            # Subscribe to folder
            try:
                M.subscribe(self.sent_folder)
            except Exception:
                pass

            # Append with \Seen flag
            date_time = imaplib.Time2Internaldate(time.time())
            typ, resp = M.append(self.sent_folder, "(\\Seen)", date_time, msg.as_bytes())
            if typ != "OK":
                raise RuntimeError(f"APPEND to {self.sent_folder} returned {typ}: {resp}")

        finally:
            try:
                M.logout()
            except Exception:
                pass
