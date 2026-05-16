"""Single source of truth for RFC 5322 sender address normalization."""

from __future__ import annotations

from email.utils import parseaddr

__all__ = ["normalize_sender_addr"]


def normalize_sender_addr(raw: str | None) -> str | None:
    """Lower-case bare address extracted from any RFC 5322 mailbox or addr-spec.

    Handles display-name forms: 'Name <addr>', '"N, N" <addr>', 'addr (comment)'.
    Returns None for empty/malformed input.

    Note: local-part comparison is case-insensitive here. RFC 5321 says
    local-part is technically case-sensitive, but every real-world MTA
    treats it case-insensitively — matching that convention.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    _name, addr = parseaddr(text)
    addr = addr.lower().strip()
    if not addr or "@" not in addr or any(ch.isspace() for ch in addr):
        return None
    return addr
