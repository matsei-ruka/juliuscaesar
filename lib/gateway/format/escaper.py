"""Convert brain output to Telegram MarkdownV2.

Two-pass algorithm:

1. Find intentional formatting spans (code fences, inline code, links,
   bold, italic, strikethrough). Replace each with a placeholder token
   so subsequent passes do not touch the contents.
2. Escape every V2-reserved char in the surrounding text. Then restore
   the placeholders with their V2-syntax replacements.

Headings (`# `, `## `) are stripped to ALL-CAPS lines (no V2 equivalent).
Bullet markers (`- `, `* `, `+ `) become `•`. Both match Rachel's
existing aesthetic and avoid the ambiguity of leading dashes.

Telegram MarkdownV2 reserved chars per the Bot API docs:
    _ * [ ] ( ) ~ ` > # + - = | { } . !

Inside `pre`/`code` entities only `` ` `` and `\\` need escaping.
Inside the URL part of an inline link only `)` and `\\` need escaping.
"""

from __future__ import annotations

import re


_RESERVED = r"_*[]()~`>#+-=|{}.!"
_RESERVED_RE = re.compile("([" + re.escape(_RESERVED) + "])")
_CODE_RESERVED_RE = re.compile(r"([`\\])")
_LINK_URL_RESERVED_RE = re.compile(r"([)\\])")

# Order of extraction matters: longer / outer patterns first so we never
# catch the inside of a code fence as if it were italic.
_FENCE_RE = re.compile(r"```([^\n`]*)\n([\s\S]*?)\n?```", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")
_BOLD_DOUBLE_STAR_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_BOLD_DOUBLE_UNDERSCORE_RE = re.compile(r"__([^_\n]+)__")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
# Single-* italic: not adjacent to alphanumerics on the outside (avoids
# catching `2*3` arithmetic). The (?<!\*) and (?!\*) prevent matching
# part of a `**bold**` span — we run after extracting bolds.
_ITALIC_STAR_RE = re.compile(r"(?<![A-Za-z0-9*])\*([^*\n]+)\*(?![A-Za-z0-9*])")
# Single-_ italic: word-boundary anchored so URLs with underscores don't
# trip it. Also requires the inner content to not start/end with whitespace.
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<![A-Za-z0-9_])_([^_\n][^_\n]*?[^_\n\s])_(?![A-Za-z0-9_])")
# Single-char `_x_` italic gets its own pattern (the above requires >=3 chars).
_ITALIC_UNDERSCORE_SHORT_RE = re.compile(r"(?<![A-Za-z0-9_])_([^_\s\n])_(?![A-Za-z0-9_])")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_BULLET_RE = re.compile(r"^(\s*)[\-\*\+]\s+", re.MULTILINE)

# Placeholder format: NUL byte + index + NUL byte. NUL never appears in
# user text from Telegram (control char). The index is plain digits so we
# can match it back trivially.
_PLACEHOLDER_OPEN = "\x00"
_PLACEHOLDER_CLOSE = "\x00"


def _escape_text(text: str) -> str:
    # Escape literal backslashes first so the reserved-char pass below
    # does not see "\" + reserved as an already-escaped sequence.
    text = text.replace("\\", "\\\\")
    return _RESERVED_RE.sub(r"\\\1", text)


def _escape_code_body(text: str) -> str:
    return _CODE_RESERVED_RE.sub(r"\\\1", text)


def _escape_link_url(url: str) -> str:
    return _LINK_URL_RESERVED_RE.sub(r"\\\1", url)


def _placeholder(index: int) -> str:
    return f"{_PLACEHOLDER_OPEN}{index}{_PLACEHOLDER_CLOSE}"


def to_markdown_v2(text: str) -> str:
    """Rewrite `text` so it is valid Telegram MarkdownV2.

    Idempotent on already-valid V2 input only when the input contains no
    bare reserved chars — i.e. it is not idempotent in general because the
    second pass would double-escape backslashes. Callers should run this
    exactly once on raw brain output.
    """
    if not text:
        return text

    spans: list[str] = []

    def _stash(rendered: str) -> str:
        spans.append(rendered)
        return _placeholder(len(spans) - 1)

    # 1. Strip CommonMark heading markers (no V2 equivalent). Uppercase the
    #    remaining heading text since that is Rachel's house style.
    def _heading_sub(match: re.Match[str]) -> str:
        body = match.group(2).strip()
        return body.upper()

    text = _HEADING_RE.sub(_heading_sub, text)

    # 2. Replace bullet markers with `• `. Done before span extraction so a
    #    bullet that happens to start with `*` does not get caught as
    #    italic. Numbered lists (`1. `) are left alone — the leading digit
    #    dot will be escaped by the reserved-char pass and renders fine.
    text = _BULLET_RE.sub(r"\1• ", text)

    # 3. Extract spans, longest/outer first.
    def _fence_sub(match: re.Match[str]) -> str:
        lang = match.group(1).strip()
        body = _escape_code_body(match.group(2))
        rendered = f"```{lang}\n{body}\n```" if lang else f"```\n{body}\n```"
        return _stash(rendered)

    text = _FENCE_RE.sub(_fence_sub, text)

    def _inline_code_sub(match: re.Match[str]) -> str:
        body = _escape_code_body(match.group(1))
        return _stash(f"`{body}`")

    text = _INLINE_CODE_RE.sub(_inline_code_sub, text)

    def _link_sub(match: re.Match[str]) -> str:
        link_text = _escape_text(match.group(1))
        url = _escape_link_url(match.group(2))
        return _stash(f"[{link_text}]({url})")

    text = _LINK_RE.sub(_link_sub, text)

    def _bold_double_star_sub(match: re.Match[str]) -> str:
        return _stash(f"*{_escape_text(match.group(1))}*")

    text = _BOLD_DOUBLE_STAR_RE.sub(_bold_double_star_sub, text)

    def _bold_double_underscore_sub(match: re.Match[str]) -> str:
        return _stash(f"*{_escape_text(match.group(1))}*")

    text = _BOLD_DOUBLE_UNDERSCORE_RE.sub(_bold_double_underscore_sub, text)

    def _strike_sub(match: re.Match[str]) -> str:
        return _stash(f"~{_escape_text(match.group(1))}~")

    text = _STRIKE_RE.sub(_strike_sub, text)

    def _italic_star_sub(match: re.Match[str]) -> str:
        return _stash(f"_{_escape_text(match.group(1))}_")

    text = _ITALIC_STAR_RE.sub(_italic_star_sub, text)

    def _italic_underscore_sub(match: re.Match[str]) -> str:
        return _stash(f"_{_escape_text(match.group(1))}_")

    text = _ITALIC_UNDERSCORE_RE.sub(_italic_underscore_sub, text)
    text = _ITALIC_UNDERSCORE_SHORT_RE.sub(_italic_underscore_sub, text)

    # 4. Escape reserved chars in everything that is left, then restore
    #    placeholders. The placeholder NUL bytes are not in the reserved
    #    set, so the escape pass leaves them alone.
    text = _escape_text(text)

    placeholder_re = re.compile(
        re.escape(_PLACEHOLDER_OPEN) + r"(\d+)" + re.escape(_PLACEHOLDER_CLOSE)
    )

    def _restore(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return spans[idx] if 0 <= idx < len(spans) else match.group(0)

    return placeholder_re.sub(_restore, text)
