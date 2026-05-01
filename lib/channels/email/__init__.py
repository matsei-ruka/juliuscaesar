"""Email channel for JuliusCaesar. IMAP/SMTP with trusted/external/blocklist senders."""

from .adapter import EmailChannelAdapter
from .authorization import SenderAuthorizer
from .imap_client import IMAPClient, EmailMessage
from .sanitize import sanitize_body, wrap_email_prompt
from .smtp_client import SMTPClient

__all__ = [
    "EmailChannelAdapter",
    "IMAPClient",
    "EmailMessage",
    "SMTPClient",
    "SenderAuthorizer",
    "sanitize_body",
    "wrap_email_prompt",
]
