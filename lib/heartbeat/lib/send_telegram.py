#!/usr/bin/env python3
"""Send a Telegram message for a JuliusCaesar instance, MarkdownV2-escaped.

Canonical Python sender. Reads body from stdin. Token + chat_id resolved
from the instance's `.env`. Applies `gateway.format.escaper.to_markdown_v2`
to the body so every sender (heartbeat, worker notifications, watchdog
status pings) renders bold/italic/code/links the same way the gateway's
`TelegramChannel.send` does. On a parse error from Telegram (HTTP 400 or
`ok=False` with `parse` in the description), retries once as plain text
so a malformed body never silently drops a notification.

Instance resolution (in order):
  1. $JC_INSTANCE_DIR
  2. Walk up from cwd for a `.jc` marker
  3. cwd if it looks like an instance (`memory/` exists)

Precedence for chat_id (highest first):
  1. --chat-id CLI flag
  2. $TELEGRAM_CHAT_ID_OVERRIDE env var
  3. $ORIGIN_CHAT_ID env var (exported by the gateway brain adapter or
     by the heartbeat runner for single-destination tasks; see
     docs/specs/origin-chat-id.md)
  4. $TELEGRAM_CHAT_ID env var          (DEPRECATED — removed next release)
  5. TELEGRAM_CHAT_ID in instance `.env` (DEPRECATED — removed next release)

When the resolved chat_id comes from one of the two deprecated sources
a one-shot warning is emitted on stderr so callers surface in logs
before the silent fallback is removed.

Precedence for bot token (highest first):
  1. --bot-token CLI flag
  2. $TELEGRAM_BOT_TOKEN env var
  3. TELEGRAM_BOT_TOKEN in instance `.env`

Exits non-zero on failure; stderr carries a short error note. Prints the
resulting `message_id` to stdout on success.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

# Allow running directly from a clone or installed shim.
_HERE = Path(__file__).resolve().parent
_FRAMEWORK = _HERE.parent.parent.parent  # heartbeat/lib/.. = heartbeat → lib → repo
_LIB = _FRAMEWORK / "lib"
if _LIB.exists() and str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from gateway.format.escaper import to_markdown_v2  # type: ignore  # noqa: E402


def _load_env_file(path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    """Tiny KEY=VALUE parser. Only loads listed keys; ignores everything else."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in keys:
            continue
        value = value.strip()
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]
        out[key] = value
    return out


def _resolve_instance_dir() -> Path:
    env = os.environ.get("JC_INSTANCE_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir():
            return p
    cur = Path.cwd().resolve()
    while True:
        if (cur / ".jc").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    cwd = Path.cwd().resolve()
    if (cwd / "memory").exists():
        return cwd
    sys.exit("send_telegram: could not resolve instance dir")


def _post(url: str, payload: dict, *, timeout: int = 20) -> tuple[int, dict]:
    """POST form-encoded payload, return (status, JSON body).

    HTTPError with a JSON body is unwrapped and returned with its real
    status code so the caller can detect parse-error 400s without
    re-raising.
    """
    body = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            return exc.code, json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            return exc.code, {"ok": False, "description": exc.reason or ""}


def _is_parse_error(status: int, payload: dict) -> bool:
    desc = str(payload.get("description") or "").lower()
    if status == 400 and ("parse" in desc or "entit" in desc):
        return True
    if not payload.get("ok") and "parse" in desc:
        return True
    return False


def send(
    body: str,
    *,
    token: str,
    chat_id: str,
    disable_web_page_preview: bool = True,
) -> str:
    """Send `body` MarkdownV2-escaped. Falls back to plain text on parse error.

    Returns the message_id as a string. Raises RuntimeError on hard failure.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    escaped = to_markdown_v2(body)
    payload = {
        "chat_id": str(chat_id),
        "text": escaped,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": "true" if disable_web_page_preview else "false",
    }
    status, data = _post(url, payload)
    if _is_parse_error(status, data):
        # Retry once as plain text so a malformed body never drops the message.
        sys.stderr.write(
            f"send_telegram: parse error ({data.get('description')!r}), "
            f"retrying without parse_mode\n"
        )
        plain = {
            "chat_id": str(chat_id),
            "text": body,
            "disable_web_page_preview": payload["disable_web_page_preview"],
        }
        status, data = _post(url, plain)
    if not data.get("ok"):
        raise RuntimeError(
            f"telegram send failed (HTTP {status}): {data.get('description') or data}"
        )
    result = data.get("result") or {}
    msg_id = result.get("message_id")
    if msg_id is None:
        raise RuntimeError(f"telegram send: no message_id in response: {data}")
    return str(msg_id)


def _load_known_chats(instance: Path) -> list[str]:
    """Return raw non-empty lines from instance/memory/L1/CHATS.md.

    Returns [] if the file doesn't exist. Used only for the verbose
    no-chat-id error message; failures are swallowed since the file
    is best-effort context, not required.
    """
    path = instance / "memory" / "L1" / "CHATS.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [ln for ln in (l.rstrip() for l in text.splitlines()) if ln.strip()]


def _format_no_chat_id_error(instance: Path, *, args) -> str:
    """Verbose, actionable failure message for the precedence ladder miss."""
    cli_state = args.chat_id if args.chat_id else "none"
    override_state = os.environ.get("TELEGRAM_CHAT_ID_OVERRIDE") or "unset"
    origin_state = os.environ.get("ORIGIN_CHAT_ID") or "unset"
    lines = [
        "[send_telegram] ERROR: no chat_id resolved.",
        f"  Checked: --chat-id ({cli_state}), TELEGRAM_CHAT_ID_OVERRIDE ({override_state}),",
        f"           ORIGIN_CHAT_ID ({origin_state})",
    ]
    chats = _load_known_chats(instance)
    if chats:
        lines.append(f"  Known chats (from {instance}/memory/L1/CHATS.md):")
        for ln in chats:
            lines.append(f"    {ln}")
    lines += [
        "  Fix one of:",
        "    - Inbound event reply: ensure the gateway brain adapter exported",
        "      ORIGIN_CHAT_ID (bug in lib/gateway/brains/base.py if absent).",
        "    - HB / cron task: add `destination:` to the task in tasks.yaml,",
        "      or wrap the call with `TELEGRAM_CHAT_ID_OVERRIDE=<id>` for",
        "      one-offs.",
        "    - Manual one-off: pass --chat-id <id> explicitly.",
    ]
    return "\n".join(lines)


def _emit_deprecated_telegram_chat_id_warning(source: str) -> None:
    """One-shot stderr warning when chat_id resolved from the legacy fallback.

    `source` is "$TELEGRAM_CHAT_ID env var" or "TELEGRAM_CHAT_ID in .env".
    See "Migration notes" in docs/specs/origin-chat-id.md.
    """
    sys.stderr.write(
        f"send_telegram: DEPRECATED — resolved chat_id from {source}. "
        "This fallback will be removed in the next release. "
        "Set ORIGIN_CHAT_ID in the caller, pass --chat-id, or export "
        "TELEGRAM_CHAT_ID_OVERRIDE.\n"
    )


def _write_push_marker(instance: Path, *, chat_id: str, message_id: str, body: str) -> None:
    marker_raw = os.environ.get("JC_PUSH_MARKER_PATH")
    if not marker_raw:
        return
    marker = Path(marker_raw).expanduser()
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "instance": str(instance),
        "channel": "telegram",
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "body_preview": body.strip()[:500],
    }
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        with marker.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="send_telegram",
        description="Send a Telegram message from stdin for a JuliusCaesar instance.",
        add_help=True,
    )
    parser.add_argument(
        "--chat-id",
        metavar="CHAT_ID",
        help="Destination chat/group/channel ID (overrides env vars and .env).",
    )
    parser.add_argument(
        "--bot-token",
        metavar="TOKEN",
        help="Bot token (overrides env vars and .env).",
    )
    args, unknown = parser.parse_known_args()
    if unknown:
        sys.stderr.write(
            f"send_telegram: unrecognized arguments ignored: {' '.join(unknown)}\n"
        )

    instance = _resolve_instance_dir()
    env_path = instance / ".env"
    env = _load_env_file(env_path, ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))

    # No os.environ rung for the bot token: a sibling instance's exported
    # TELEGRAM_BOT_TOKEN is the cross-instance impersonation vector (audit
    # G-P1 / feature 8). Explicit --bot-token and the instance .env are the
    # only sources. The chat-id ladder below keeps its env rungs — those are
    # per-invocation routing set by the gateway itself, not auth.
    token = args.bot_token or env.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit(f"send_telegram: TELEGRAM_BOT_TOKEN not set (define in {env_path})")

    # Precedence ladder per docs/specs/origin-chat-id.md. The two legacy
    # TELEGRAM_CHAT_ID sources stay for one release with a deprecation
    # warning so misrouting callers surface in logs before removal.
    chat_id: str | None = None
    if args.chat_id:
        chat_id = args.chat_id
    elif os.environ.get("TELEGRAM_CHAT_ID_OVERRIDE"):
        chat_id = os.environ["TELEGRAM_CHAT_ID_OVERRIDE"]
    elif os.environ.get("ORIGIN_CHAT_ID"):
        chat_id = os.environ["ORIGIN_CHAT_ID"]
    elif os.environ.get("TELEGRAM_CHAT_ID"):
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
        _emit_deprecated_telegram_chat_id_warning("$TELEGRAM_CHAT_ID env var")
    elif env.get("TELEGRAM_CHAT_ID"):
        chat_id = env["TELEGRAM_CHAT_ID"]
        _emit_deprecated_telegram_chat_id_warning(
            f"TELEGRAM_CHAT_ID in {env_path}"
        )

    if not chat_id:
        sys.stderr.write(_format_no_chat_id_error(instance, args=args) + "\n")
        return 1

    body = sys.stdin.read()
    if not body.strip():
        sys.exit("send_telegram: empty body, refusing to send")

    try:
        msg_id = send(body, token=token, chat_id=chat_id)
    except RuntimeError as exc:
        sys.stderr.write(f"send_telegram: {exc}\n")
        return 1
    _write_push_marker(instance, chat_id=chat_id, message_id=msg_id, body=body)
    print(msg_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
