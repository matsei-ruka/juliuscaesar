"""Codex auth extractor — direct OpenAI API access via the local Codex CLI's
OAuth state.

Public surface:
- ``CodexAuthClient`` — refreshes / serves bearer tokens
- ``ResponsesClient``  — thin wrapper around POST /v1/responses
- exception types in :mod:`codex_auth.errors`

See ``docs/specs/codex-auth-extractor.md`` for design rationale.
"""

from __future__ import annotations

from .client import CodexAuthClient, AuthState, default_auth_path
from .errors import (
    CodexAuthError,
    AuthFileMissing,
    AuthModeUnsupported,
    AuthFileCorrupt,
    RefreshExpired,
    RefreshFailed,
    ReloginRequired,
)
from .responses import ResponsesClient, ResponsesError

__all__ = [
    "CodexAuthClient",
    "AuthState",
    "default_auth_path",
    "ResponsesClient",
    "ResponsesError",
    "CodexAuthError",
    "AuthFileMissing",
    "AuthModeUnsupported",
    "AuthFileCorrupt",
    "RefreshExpired",
    "RefreshFailed",
    "ReloginRequired",
]
