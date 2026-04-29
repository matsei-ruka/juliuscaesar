"""Typed exceptions for the codex auth extractor."""

from __future__ import annotations


class CodexAuthError(Exception):
    """Base class for all codex auth failures."""


class AuthFileMissing(CodexAuthError):
    """``auth.json`` not found — the operator has not run ``codex login``."""


class AuthFileCorrupt(CodexAuthError):
    """``auth.json`` exists but is unparseable or missing required fields."""


class AuthModeUnsupported(CodexAuthError):
    """``auth_mode`` is not ``chatgpt`` (e.g. legacy API-key mode)."""


class RefreshFailed(CodexAuthError):
    """Network / 5xx / unknown-error response from the OAuth refresh endpoint."""


class RefreshExpired(CodexAuthError):
    """Refresh token rejected (``invalid_grant`` etc.) — re-login required.

    Operators must run ``codex login`` again. Carries the raw error code so
    callers can show it without re-parsing.
    """

    def __init__(self, code: str, description: str = ""):
        self.code = code
        self.description = description
        super().__init__(f"{code}: {description}" if description else code)


# Alias kept for spec parity ("ReloginRequired" is the spec's preferred name).
ReloginRequired = RefreshExpired
