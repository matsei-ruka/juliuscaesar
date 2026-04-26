"""Outbound formatting helpers.

`to_markdown_v2(text)` rewrites brain output (CommonMark-ish markdown +
plain prose) into Telegram MarkdownV2: intentional formatting spans are
preserved using V2 syntax, every reserved char in surrounding text is
backslash-escaped so Telegram does not 400 on a bare `.` or `-`.
"""

from .escaper import to_markdown_v2


__all__ = ["to_markdown_v2"]
