"""``jc-codex-auth`` CLI implementation.

Subcommands:
- ``status``           Show plan, account, ttl, last_refresh, file mode.
- ``refresh [--force]`` Refresh now (skip if not expiring soon unless --force).
- ``token``            Print a fresh bearer token to stdout.

Exit codes:
- 0: success
- 2: actionable user error (auth.json missing, wrong auth_mode, re-login)
- 1: unexpected failure
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .client import CodexAuthClient, DEFAULT_SKEW_SECONDS, default_auth_path
from .errors import (
    AuthFileCorrupt,
    AuthFileMissing,
    AuthModeUnsupported,
    RefreshExpired,
    RefreshFailed,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jc-codex-auth",
        description="Inspect / refresh the local Codex CLI's OAuth state.",
    )
    p.add_argument(
        "--auth-file",
        default=None,
        help="Path to auth.json (default: ~/.codex/auth.json)",
    )
    p.add_argument(
        "--client-id",
        default=None,
        help="OAuth client_id override (default: read from JWT, fall back to bundled constant)",
    )
    p.add_argument(
        "--refresh-skew-seconds",
        type=int,
        default=DEFAULT_SKEW_SECONDS,
        help="Refresh this far ahead of expiry (default: %(default)s)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status", help="Show auth status and time-to-expiry")
    status.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text")

    refresh = sub.add_parser("refresh", help="Refresh the bearer token")
    refresh.add_argument("--force", action="store_true", help="Refresh even if not expiring soon")

    sub.add_parser("token", help="Print a fresh bearer token to stdout")
    return p


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds <= 0:
        return f"expired {int(-seconds)}s ago"
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, secs = divmod(s, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _print_status(snapshot: dict, json_out: bool) -> None:
    if json_out:
        sys.stdout.write(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
        return
    plan = snapshot.get("plan") or "?"
    account = snapshot.get("account_id") or "?"
    client_id = snapshot.get("client_id") or "?"
    ttl = snapshot.get("expires_in_seconds")
    last = snapshot.get("last_refresh") or "?"
    auth_file = snapshot.get("auth_file")
    mode = snapshot.get("auth_file_mode")
    mode_str = f"{mode:o}" if isinstance(mode, int) else "?"
    mode_ok = "✓" if mode == 0o600 else "⚠"
    sys.stdout.write(
        f"Auth mode:     {snapshot.get('auth_mode') or '?'} ({plan})\n"
        f"Account:       {account}\n"
        f"Client ID:     {client_id}\n"
        f"Access token:  {'valid' if (ttl is not None and ttl > 0) else 'expired'}, "
        f"expires in {_format_duration(ttl)}\n"
        f"Last refresh:  {last}\n"
        f"Auth file:     {auth_file} (mode {mode_str} {mode_ok})\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    auth_file = Path(args.auth_file).expanduser() if args.auth_file else default_auth_path()
    client = CodexAuthClient(
        auth_file=auth_file,
        client_id_override=args.client_id,
        refresh_skew_seconds=args.refresh_skew_seconds,
    )
    try:
        if args.cmd == "status":
            _print_status(client.status(), getattr(args, "json", False))
            return 0
        if args.cmd == "refresh":
            if args.force:
                state = client.force_refresh()
            else:
                # Fast path: get_bearer() refreshes only if needed; we then
                # report the post-refresh expiry so the operator knows.
                client.get_bearer()
                state = client.read_state()
            ttl = state.access_token_expiry
            now = time.time()
            remaining = (ttl - now) if ttl is not None else None
            sys.stdout.write(
                f"Refreshed. Access token expires in {_format_duration(remaining)}.\n"
            )
            return 0
        if args.cmd == "token":
            sys.stdout.write(client.get_bearer() + "\n")
            return 0
    except AuthFileMissing as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    except AuthModeUnsupported as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    except AuthFileCorrupt as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    except RefreshExpired as exc:
        sys.stderr.write(f"refresh rejected ({exc.code}); run `codex login` to re-authenticate.\n")
        return 2
    except RefreshFailed as exc:
        sys.stderr.write(f"refresh failed: {exc}\n")
        return 1
    parser.error("unknown subcommand")
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
