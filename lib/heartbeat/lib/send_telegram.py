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

Env precedence for chat_id:
  1. $TELEGRAM_CHAT_ID_OVERRIDE
  2. $TELEGRAM_CHAT_ID from the instance's `.env`

Exits non-zero on failure; stderr carries a short error note. Prints the
resulting `message_id` to stdout on success.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
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


def main() -> int:
    instance = _resolve_instance_dir()
    env_path = instance / ".env"
    env = _load_env_file(env_path, ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))

    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit(f"send_telegram: TELEGRAM_BOT_TOKEN not set (define in {env_path})")

    chat_id = (
        os.environ.get("TELEGRAM_CHAT_ID_OVERRIDE")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or env.get("TELEGRAM_CHAT_ID")
    )
    if not chat_id:
        sys.exit(
            f"send_telegram: TELEGRAM_CHAT_ID not set "
            f"(define in {env_path} or pass TELEGRAM_CHAT_ID_OVERRIDE)"
        )

    body = sys.stdin.read()
    if not body.strip():
        sys.exit("send_telegram: empty body, refusing to send")

    try:
        msg_id = send(body, token=token, chat_id=chat_id)
    except RuntimeError as exc:
        sys.stderr.write(f"send_telegram: {exc}\n")
        return 1
    print(msg_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
