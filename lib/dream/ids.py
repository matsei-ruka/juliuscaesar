"""Stable ids for dream artifacts."""

from __future__ import annotations

import hashlib


def diff_id(*parts: str) -> str:
    body = "\n".join(parts)
    return "dream-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def slugify(value: str, *, fallback: str = "dream-artifact", max_len: int = 64) -> str:
    out = []
    last_dash = False
    for ch in value.lower():
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash:
            out.append("-")
            last_dash = True
    slug = "".join(out).strip("-") or fallback
    return slug[:max_len].rstrip("-") or fallback
