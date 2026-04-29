"""OAuth refresh + bearer-token retrieval for the local Codex CLI auth state.

The Codex CLI persists OAuth state to ``~/.codex/auth.json``. Its
``access_token`` is a JWT already valid as ``Authorization: Bearer`` for
``api.openai.com/v1/*`` — no token exchange required.

This module reads, refreshes (when expiry is near), and writes that file
under a sibling ``fcntl.flock`` so we never fight the Codex CLI's own lock.
Writes are atomic (``tempfile`` + ``os.replace``) and preserve mode 0600.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import json
import os
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    AuthFileCorrupt,
    AuthFileMissing,
    AuthModeUnsupported,
    RefreshExpired,
    RefreshFailed,
)


# Public OAuth client ID published by the Codex CLI binary. We accept an
# override at runtime (env var `CODEX_CLIENT_ID` or per-instance config) so a
# Codex update that rotates the ID never silently breaks us.
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_URL = "https://auth.openai.com/oauth/token"
DEFAULT_SKEW_SECONDS = 300
LOCK_TIMEOUT_SECONDS = 10
REFRESH_TIMEOUT_SECONDS = 20
RETRY_BACKOFF_SECONDS = (1.0, 3.0, 7.0)

_REFRESH_EXPIRED_CODES = frozenset(
    {
        "invalid_grant",
        "refresh_token_expired",
        "refresh_token_already_used",
        "refresh_token_revoked",
    }
)


def default_auth_path() -> Path:
    return Path.home() / ".codex" / "auth.json"


@dataclass(frozen=True)
class AuthState:
    """In-memory view of ``auth.json``.

    Only the fields we actually use are typed; the rest of the JSON document
    is preserved verbatim in :attr:`raw` and re-emitted on write so we never
    drop unknown keys the Codex CLI may rely on.
    """

    auth_mode: str
    access_token: str
    id_token: str | None
    refresh_token: str
    account_id: str | None
    last_refresh: str | None
    raw: dict[str, Any]

    @property
    def access_token_expiry(self) -> float | None:
        return jwt_unverified_exp(self.access_token)

    @property
    def access_token_client_id(self) -> str | None:
        payload = jwt_unverified_payload(self.access_token)
        if not isinstance(payload, dict):
            return None
        cid = payload.get("client_id")
        return str(cid) if isinstance(cid, str) else None

    @property
    def chatgpt_plan_type(self) -> str | None:
        payload = jwt_unverified_payload(self.access_token) or {}
        info = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else None
        if isinstance(info, dict):
            plan = info.get("chatgpt_plan_type")
            return str(plan) if isinstance(plan, str) else None
        return None

    @property
    def chatgpt_account_id(self) -> str | None:
        payload = jwt_unverified_payload(self.access_token) or {}
        info = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else None
        if isinstance(info, dict):
            acc = info.get("chatgpt_account_id")
            if isinstance(acc, str):
                return acc
        return self.account_id


# --- JWT helpers -------------------------------------------------------------

def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def jwt_unverified_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload without verifying its signature.

    We are not the auth server; we just need to read ``exp`` / ``client_id``
    so we know when to refresh and which client_id this token was issued to.
    """
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload_bytes = _b64url_decode(parts[1])
        data = json.loads(payload_bytes)
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def jwt_unverified_exp(token: str) -> float | None:
    payload = jwt_unverified_payload(token)
    if not payload:
        return None
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return float(exp)
    return None


# --- AuthState (de)serialization --------------------------------------------

def _parse_auth_json(blob: str | bytes, path: Path) -> AuthState:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise AuthFileCorrupt(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise AuthFileCorrupt(f"{path}: expected a JSON object")
    auth_mode = str(data.get("auth_mode") or "")
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        raise AuthFileCorrupt(f"{path}: missing 'tokens' object")
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not isinstance(access, str) or not access:
        raise AuthFileCorrupt(f"{path}: tokens.access_token missing")
    if not isinstance(refresh, str) or not refresh:
        raise AuthFileCorrupt(f"{path}: tokens.refresh_token missing")
    return AuthState(
        auth_mode=auth_mode,
        access_token=access,
        id_token=str(tokens.get("id_token")) if tokens.get("id_token") else None,
        refresh_token=refresh,
        account_id=str(tokens.get("account_id")) if tokens.get("account_id") else None,
        last_refresh=str(data.get("last_refresh")) if data.get("last_refresh") else None,
        raw=data,
    )


def _serialize_auth_state(state: AuthState) -> bytes:
    payload = json.dumps(state.raw, indent=2, sort_keys=False) + "\n"
    return payload.encode("utf-8")


def _state_with_new_tokens(
    state: AuthState,
    *,
    access_token: str,
    id_token: str | None,
    refresh_token: str | None,
    last_refresh_iso: str,
) -> AuthState:
    raw = json.loads(json.dumps(state.raw))
    tokens = raw.setdefault("tokens", {})
    tokens["access_token"] = access_token
    if id_token is not None:
        tokens["id_token"] = id_token
    if refresh_token is not None:
        tokens["refresh_token"] = refresh_token
    raw["last_refresh"] = last_refresh_iso
    return _parse_auth_json(json.dumps(raw), Path("<memory>"))


# --- Locking -----------------------------------------------------------------

@contextlib.contextmanager
def _file_lock(lock_path: Path, *, exclusive: bool, timeout: float):
    """Acquire an ``fcntl.flock`` with a polling timeout.

    The lock file is a sibling of ``auth.json`` (e.g. ``auth.json.lock``) so
    we never contend with the Codex CLI's own lock on ``auth.json``.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, flag | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out acquiring {'exclusive' if exclusive else 'shared'} lock on {lock_path}"
                    ) from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Atomic replace via tempfile in the same directory + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".auth-", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# --- Refresh -----------------------------------------------------------------

class _UrlOpener:
    """Indirection so tests can replace the network call without monkey-patching urllib.

    Returns ``(status, body_bytes)``.
    """

    def post_form(self, url: str, body: dict[str, str], timeout: float) -> tuple[int, bytes]:
        encoded = urllib.parse.urlencode(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            data = b""
            try:
                data = exc.read()
            except Exception:  # noqa: BLE001
                pass
            return exc.code, data


# --- Client ------------------------------------------------------------------

class CodexAuthClient:
    """High-level facade: ``get_bearer()`` is the canonical entry point."""

    def __init__(
        self,
        *,
        auth_file: Path | str | None = None,
        client_id_override: str | None = None,
        refresh_skew_seconds: int = DEFAULT_SKEW_SECONDS,
        lock_timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
        refresh_timeout_seconds: float = REFRESH_TIMEOUT_SECONDS,
        opener: _UrlOpener | None = None,
        clock: callable = time.time,
    ):
        self.auth_file = Path(auth_file).expanduser() if auth_file else default_auth_path()
        self.lock_file = self.auth_file.with_name(self.auth_file.name + ".lock")
        self.client_id_override = client_id_override or os.environ.get("CODEX_CLIENT_ID") or None
        self.refresh_skew_seconds = int(refresh_skew_seconds)
        self.lock_timeout_seconds = float(lock_timeout_seconds)
        self.refresh_timeout_seconds = float(refresh_timeout_seconds)
        self._opener = opener or _UrlOpener()
        self._clock = clock

    # --- public API --------------------------------------------------------

    def read_state(self) -> AuthState:
        if not self.auth_file.exists():
            raise AuthFileMissing(
                f"{self.auth_file} not found — run `codex login` first."
            )
        with _file_lock(self.lock_file, exclusive=False, timeout=self.lock_timeout_seconds):
            blob = self.auth_file.read_bytes()
        state = _parse_auth_json(blob, self.auth_file)
        self._enforce_chatgpt_mode(state)
        return state

    def status(self) -> dict[str, Any]:
        """Operator-facing snapshot. Never includes raw token material."""
        state = self.read_state()
        exp = state.access_token_expiry
        now = self._clock()
        ttl = (exp - now) if exp is not None else None
        try:
            file_mode = stat.S_IMODE(self.auth_file.stat().st_mode)
        except OSError:
            file_mode = None
        return {
            "auth_mode": state.auth_mode,
            "plan": state.chatgpt_plan_type,
            "account_id": state.chatgpt_account_id,
            "client_id": state.access_token_client_id,
            "expires_in_seconds": ttl,
            "expires_at": exp,
            "last_refresh": state.last_refresh,
            "auth_file": str(self.auth_file),
            "auth_file_mode": file_mode,
            "refresh_skew_seconds": self.refresh_skew_seconds,
        }

    def get_bearer(self) -> str:
        """Return a bearer token, refreshing only if expiry is within skew."""
        state = self.read_state()
        if not self._needs_refresh(state):
            return state.access_token
        return self._refresh(state).access_token

    def force_refresh(self) -> AuthState:
        """Refresh now regardless of current expiry."""
        state = self.read_state()
        return self._refresh(state, force=True)

    # --- internals --------------------------------------------------------

    def _enforce_chatgpt_mode(self, state: AuthState) -> None:
        if state.auth_mode and state.auth_mode != "chatgpt":
            raise AuthModeUnsupported(
                f"auth_mode={state.auth_mode!r}: only 'chatgpt' subscription auth is supported."
            )

    def _needs_refresh(self, state: AuthState) -> bool:
        exp = state.access_token_expiry
        if exp is None:
            return True
        return (self._clock() + self.refresh_skew_seconds) >= exp

    def _resolve_client_id(self, state: AuthState) -> str:
        return (
            self.client_id_override
            or state.access_token_client_id
            or DEFAULT_CLIENT_ID
        )

    def _refresh(self, prior: AuthState, *, force: bool = False) -> AuthState:
        with _file_lock(self.lock_file, exclusive=True, timeout=self.lock_timeout_seconds):
            # Double-checked: another process may have refreshed while we
            # waited for the lock. Re-read and re-evaluate.
            blob = self.auth_file.read_bytes()
            state = _parse_auth_json(blob, self.auth_file)
            self._enforce_chatgpt_mode(state)
            if not force and not self._needs_refresh(state):
                return state
            new_tokens = self._post_refresh(state)
            updated = _state_with_new_tokens(
                state,
                access_token=new_tokens["access_token"],
                id_token=new_tokens.get("id_token"),
                refresh_token=new_tokens.get("refresh_token") or state.refresh_token,
                last_refresh_iso=_now_iso(self._clock),
            )
            _atomic_write(self.auth_file, _serialize_auth_state(updated))
            return updated

    def _post_refresh(self, state: AuthState) -> dict[str, Any]:
        body = {
            "grant_type": "refresh_token",
            "refresh_token": state.refresh_token,
            "client_id": self._resolve_client_id(state),
        }
        last_exc: Exception | None = None
        for attempt, backoff in enumerate((*RETRY_BACKOFF_SECONDS, None)):
            try:
                status, raw = self._opener.post_form(
                    REFRESH_URL, body, self.refresh_timeout_seconds
                )
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_exc = exc
                if backoff is None:
                    raise RefreshFailed(f"network error: {exc}") from exc
                time.sleep(backoff)
                continue
            payload = _safe_json(raw)
            if 200 <= status < 300:
                if not isinstance(payload, dict) or "access_token" not in payload:
                    raise RefreshFailed(
                        f"refresh OK ({status}) but response missing access_token"
                    )
                return payload
            err_code = ""
            err_desc = ""
            if isinstance(payload, dict):
                err_code = str(payload.get("error") or "")
                err_desc = str(payload.get("error_description") or "")
            if err_code in _REFRESH_EXPIRED_CODES:
                raise RefreshExpired(err_code, err_desc)
            if 400 <= status < 500 and status != 408 and status != 429:
                raise RefreshFailed(
                    f"refresh failed ({status}): {err_code or raw[:200]!r}"
                )
            last_exc = RefreshFailed(f"refresh transient {status}: {err_code or 'unknown'}")
            if backoff is None:
                raise last_exc
            time.sleep(backoff)
        # Unreachable: loop always returns or raises.
        raise RefreshFailed(str(last_exc) if last_exc else "refresh failed")


def _safe_json(blob: bytes) -> Any:
    if not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def _now_iso(clock: callable = time.time) -> str:
    from datetime import datetime, timezone

    ts = float(clock())
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
