"""Soft-import DKIM verification helpers — disable gracefully if `dkimpy` absent."""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - presence depends on optional dep
    import dkim as _dkim  # type: ignore
except Exception:  # noqa: BLE001
    _dkim = None


def dkim_available() -> bool:
    return _dkim is not None


def verify_message(raw_message: bytes) -> tuple[bool, str]:
    """Verify a raw RFC822 message against published DKIM keys.

    Returns ``(passed, reason)``. ``passed`` is False if dkimpy is missing or
    verification raised; ``reason`` is a short label suitable for logging.
    """
    if _dkim is None:
        return False, "dkimpy_not_installed"
    try:
        ok = bool(_dkim.verify(raw_message))
    except Exception as exc:  # noqa: BLE001
        return False, f"dkim_error:{exc.__class__.__name__}"
    return ok, "dkim_pass" if ok else "dkim_fail"


def signing_domain(parsed_message: Any) -> str | None:
    """Best-effort extract of the `d=` tag from a DKIM-Signature header."""
    header = parsed_message.get("DKIM-Signature") if parsed_message else None
    if not header:
        return None
    for chunk in str(header).split(";"):
        chunk = chunk.strip()
        if chunk.startswith("d="):
            return chunk[2:].strip().rstrip(">").lstrip("<")
    return None


def authentication_results_pass(
    parsed_message: Any, trusted_mta_hostnames: tuple[str, ...]
) -> bool:
    """Accept an MTA-provided Authentication-Results header when from a trusted host."""
    if parsed_message is None:
        return False
    headers = parsed_message.get_all("Authentication-Results") or []
    for header in headers:
        text = str(header).strip()
        host = text.split(";", 1)[0].strip().lower()
        if host in {h.lower() for h in trusted_mta_hostnames} and "dkim=pass" in text.lower():
            return True
    return False
