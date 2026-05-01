"""Injection prevention: HTML stripping, body truncation, sender wrapping."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Optional

__all__ = ["sanitize_body", "wrap_email_prompt"]


class HTMLStripper(HTMLParser):
    """Extract text content from HTML (strip all tags)."""

    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.text_parts: list[str] = []

    def handle_data(self, d: str) -> None:
        self.text_parts.append(d)

    def get_text(self) -> str:
        return "".join(self.text_parts)


def strip_html(html: str) -> str:
    """Convert HTML to plain text (strip all tags, preserve text content)."""
    try:
        stripper = HTMLStripper()
        stripper.feed(html)
        return stripper.get_text()
    except Exception:
        # Fallback: regex-based stripping
        return re.sub(r"<[^>]+>", "", html)


def sanitize_body(
    body: str,
    is_html: bool = False,
    max_chars: int = 8000,
) -> tuple[str, bool]:
    """Sanitize email body for LLM consumption.

    Returns (sanitized_text, was_truncated).
    """
    # Strip HTML if needed
    if is_html:
        body = strip_html(body)

    # Normalize whitespace but preserve line breaks
    body = body.strip()

    # Check truncation
    was_truncated = False
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n\n[… message truncated at {max_chars} chars …]"
        was_truncated = True

    return body, was_truncated


def wrap_email_prompt(
    sender: str,
    subject: str,
    body: str,
    is_html: bool = False,
    max_chars: int = 8000,
) -> str:
    """Wrap email body with sender/subject header for LLM.

    Format:
    [EMAIL from <sender>, subject: "<subject>"]

    <body>
    """
    # Sanitize body
    sanitized_body, was_truncated = sanitize_body(body, is_html, max_chars)

    # Strip newlines from sender/subject to prevent prompt injection
    # (RFC 5322 allows encoded newlines; we normalize to single-line display)
    safe_sender = sender.replace("\r", "").replace("\n", " ").strip()
    safe_subject = subject.replace("\r", "").replace("\n", " ").strip()

    # Build prompt
    header = f'[EMAIL from {safe_sender}, subject: "{safe_subject}"]'
    prompt = f"{header}\n\n{sanitized_body}"

    return prompt
