"""IMAP client for email channel. Wraps imaplib with connection pooling,
UID watermarking, multipart extraction, charset normalization."""

from __future__ import annotations

import email
import email.header
import imaplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

__all__ = ["IMAPClient", "EmailMessage"]


@dataclass
class EmailMessage:
    """Parsed email message."""
    uid: str
    sender: str
    sender_name: str
    to: list[str]
    cc: list[str]
    subject: str
    date: str
    message_id: str
    in_reply_to: Optional[str]
    references: list[str]
    body: str
    is_unread: bool


class IMAPClient:
    """IMAP4_SSL wrapper with readonly select, BODY.PEEK (no side effects)."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 993,
        timeout: int = 30,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.timeout = timeout
        self.conn: Optional[imaplib.IMAP4_SSL] = None

    def connect(self) -> None:
        """Open SSL connection."""
        ctx = ssl.create_default_context()
        self.conn = imaplib.IMAP4_SSL(
            self.host, self.port, ssl_context=ctx, timeout=self.timeout
        )
        self.conn.login(self.user, self.password)

    def disconnect(self) -> None:
        """Close connection gracefully."""
        if self.conn:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def _ensure_connected(self) -> None:
        if not self.conn:
            self.connect()

    def select_readonly(self, mailbox: str = "INBOX") -> None:
        """Select mailbox in readonly mode (no server-side side effects)."""
        self._ensure_connected()
        self.conn.select(mailbox, readonly=True)

    def fetch_new(self, since_uid: int, mailbox: str = "INBOX") -> list[EmailMessage]:
        """Fetch messages with UID > since_uid. Returns newest first."""
        self._ensure_connected()
        self.select_readonly(mailbox)

        if since_uid > 0:
            search_criterion = f"UID {since_uid + 1}:*"
        else:
            search_criterion = "ALL"

        typ, data = self.conn.uid("SEARCH", None, search_criterion)
        if typ != "OK" or not data or not data[0]:
            return []

        uids = data[0].split()
        if not uids:
            return []

        # Newest first
        uids = list(reversed(uids))
        return self._fetch_messages(uids)

    def fetch_unread(self, mailbox: str = "INBOX") -> list[EmailMessage]:
        """Fetch all UNSEEN messages."""
        self._ensure_connected()
        self.select_readonly(mailbox)

        typ, data = self.conn.uid("SEARCH", None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return []

        uids = data[0].split()
        if not uids:
            return []

        uids = list(reversed(uids))
        return self._fetch_messages(uids)

    def _fetch_messages(self, uids: list[bytes]) -> list[EmailMessage]:
        """Fetch full messages for given UIDs using BODY.PEEK."""
        messages = []
        for uid in uids:
            try:
                typ, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[] FLAGS)")
                if typ != "OK" or not data or not data[0]:
                    continue

                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                # Parse flags
                flags_blob = b""
                if len(data) > 1 and isinstance(data[-1], bytes):
                    flags_blob = data[-1]
                flags_str = flags_blob.decode("utf-8", errors="replace")
                is_unread = "\\Seen" not in flags_str

                # Parse headers
                sender = self._decode_header(msg.get("From", ""))
                sender_name = self._extract_name(sender)
                to = [self._decode_header(msg.get("To", ""))]
                cc_raw = msg.get("Cc", "")
                cc = [self._decode_header(cc_raw)] if cc_raw else []
                subject = self._decode_header(msg.get("Subject", "(no subject)"))
                message_id = msg.get("Message-ID", "").strip()
                in_reply_to = msg.get("In-Reply-To", "").strip() or None
                references_raw = msg.get("References", "").strip()
                references = [r.strip() for r in references_raw.split() if r.strip()] if references_raw else []

                # Parse date
                date_raw = msg.get("Date", "")
                try:
                    dt = parsedate_to_datetime(date_raw)
                    date_iso = dt.astimezone().isoformat(timespec="seconds")
                except (TypeError, ValueError):
                    date_iso = date_raw

                # Extract body (text only)
                body = self._extract_text_body(msg)

                messages.append(EmailMessage(
                    uid=uid.decode(),
                    sender=sender,
                    sender_name=sender_name,
                    to=to,
                    cc=cc,
                    subject=subject,
                    date=date_iso,
                    message_id=message_id,
                    in_reply_to=in_reply_to,
                    references=references,
                    body=body,
                    is_unread=is_unread,
                ))
            except Exception:
                # Skip malformed messages
                continue

        return messages

    @staticmethod
    def _decode_header(raw: str) -> str:
        """Decode RFC 2047 encoded headers."""
        if not raw:
            return ""
        parts = email.header.decode_header(raw)
        chunks = []
        for text, charset in parts:
            if isinstance(text, bytes):
                try:
                    chunks.append(text.decode(charset or "utf-8", errors="replace"))
                except (LookupError, TypeError):
                    chunks.append(text.decode("utf-8", errors="replace"))
            else:
                chunks.append(text)
        return "".join(chunks).strip()

    @staticmethod
    def _extract_name(email_str: str) -> str:
        """Extract display name from 'Display Name <email@addr>' format."""
        if "<" in email_str:
            name = email_str.split("<")[0].strip().strip('"')
            return name if name else email_str
        return email_str

    @staticmethod
    def _extract_text_body(msg: email.message.Message) -> str:
        """Extract plain-text body from message (prefer text/plain, fallback to HTML)."""
        body = ""

        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = (part.get("Content-Disposition") or "").lower()
                if "attachment" in disp:
                    continue

                if ctype == "text/plain":
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body = payload.decode(charset, errors="replace")
                    except LookupError:
                        body = payload.decode("utf-8", errors="replace")
                    return body

            # Fallback to HTML (will be stripped by sanitizer)
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body = payload.decode(charset, errors="replace")
                    except LookupError:
                        body = payload.decode("utf-8", errors="replace")
                    return body
        else:
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except LookupError:
                body = payload.decode("utf-8", errors="replace")

        return body
