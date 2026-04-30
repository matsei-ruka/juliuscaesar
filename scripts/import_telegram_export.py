#!/usr/bin/env python3
"""Import a Telegram desktop chat export (result.json) into transcripts.

Telegram desktop's "Export chat history" produces a JSON with:

    {
      "name": "...", "type": "personal_chat|bot_chat|...",
      "id": <other_party_id>,
      "messages": [
        {"id": <int>, "type": "message"|"service",
         "date": "...", "date_unixtime": "...",
         "from": "...", "from_id": "user<id>",
         "text": "..." | [str | {"type": "...", "text": "..."}],
         "reply_to_message_id": <int>?, ...},
        ...
      ]
    }

We map each ``type=message`` entry into a `gateway.transcripts` row
(``state/transcripts/<conversation_id>.jsonl``):

- ``role`` = "user" if from_id matches the human, else "assistant"
- ``ts``   = ISO-8601 UTC from ``date_unixtime``
- ``text`` = flattened ``text`` (entities → their literal ``text`` field)
- ``message_id`` = str(``id``)

Dedupe: existing message_ids in the target file are skipped, so the
script is safe to re-run.

Usage:

    scripts/import_telegram_export.py \\
        --instance-dir /home/lucamattei/rachel_zane \\
        --export       /path/to/result.json \\
        --conversation-id 28547271 \\
        --user-id 28547271             # (optional; auto-detected)

If ``--user-id`` is omitted, the script picks the most-frequent
non-bot ``from_id`` in the export (where the bot is the top-level
``id``). Override when the export contains multiple humans.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LIB = _HERE.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from gateway import transcripts  # type: ignore  # noqa: E402


def _flatten_text(value) -> str:
    """Telegram exports may store text as a string or a mixed array."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        out: list[str] = []
        for chunk in value:
            if isinstance(chunk, str):
                out.append(chunk)
            elif isinstance(chunk, dict):
                t = chunk.get("text")
                if isinstance(t, str):
                    out.append(t)
        return "".join(out)
    return ""


def _ts_iso(unixtime: str | int | None, fallback_iso: str | None) -> str:
    if unixtime is not None:
        try:
            dt = datetime.fromtimestamp(int(unixtime), tz=timezone.utc)
            return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        except (TypeError, ValueError):
            pass
    if fallback_iso:
        # Local-time string in export; assume UTC if no tz suffix.
        try:
            dt = datetime.fromisoformat(fallback_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _existing_message_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for ev in transcripts.iter_events(path):
        if ev.message_id:
            out.add(ev.message_id)
    return out


def _detect_user_id(messages: list[dict], bot_from_id: str) -> str | None:
    counts: Counter[str] = Counter()
    for m in messages:
        if m.get("type") != "message":
            continue
        fid = m.get("from_id")
        if isinstance(fid, str) and fid != bot_from_id:
            counts[fid] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--instance-dir", required=True, type=Path)
    p.add_argument("--export", required=True, type=Path)
    p.add_argument("--conversation-id", required=True)
    p.add_argument(
        "--user-id",
        default=None,
        help="numeric Telegram user id of the human (auto-detected if omitted)",
    )
    p.add_argument(
        "--bot-id",
        default=None,
        help="numeric Telegram bot id (defaults to top-level export id)",
    )
    p.add_argument("--channel", default="telegram")
    p.add_argument("--chat-id", default=None,
                   help="chat_id stamped on each row (defaults to --conversation-id)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    data = json.loads(args.export.read_text(encoding="utf-8"))
    messages = data.get("messages") or []
    if not isinstance(messages, list):
        sys.exit("export contains no 'messages' array")

    bot_id = args.bot_id or str(data.get("id") or "")
    if not bot_id:
        sys.exit("could not determine bot id (pass --bot-id)")
    bot_from_id = f"user{bot_id}"

    user_id = args.user_id or ""
    if not user_id:
        detected = _detect_user_id(messages, bot_from_id)
        if not detected:
            sys.exit("could not auto-detect human user id (pass --user-id)")
        # `from_id` is "user<digits>"
        user_id = detected[len("user"):] if detected.startswith("user") else detected
    user_from_id = f"user{user_id}"

    chat_id = args.chat_id or args.conversation_id
    target = transcripts.transcript_path(args.instance_dir, args.conversation_id)
    seen = _existing_message_ids(target)

    appended = skipped_dupe = skipped_empty = skipped_service = skipped_other = 0
    for m in messages:
        if m.get("type") != "message":
            skipped_service += 1
            continue
        msg_id = m.get("id")
        if msg_id is None:
            skipped_other += 1
            continue
        msg_id_s = str(msg_id)
        if msg_id_s in seen:
            skipped_dupe += 1
            continue
        text = _flatten_text(m.get("text", "")).strip()
        if not text:
            skipped_empty += 1
            continue
        fid = m.get("from_id")
        if fid == bot_from_id:
            role = "assistant"
        elif fid == user_from_id:
            role = "user"
        else:
            skipped_other += 1
            continue
        ts = _ts_iso(m.get("date_unixtime"), m.get("date"))
        if args.dry_run:
            print(f"[{ts}] {role} {msg_id_s} {text[:60]!r}")
            appended += 1
            continue
        transcripts.append(
            args.instance_dir,
            conversation_id=args.conversation_id,
            role=role,
            text=text,
            message_id=msg_id_s,
            channel=args.channel,
            chat_id=chat_id,
            ts=ts,
        )
        seen.add(msg_id_s)
        appended += 1

    print(
        f"appended={appended} skipped_dupe={skipped_dupe} "
        f"skipped_empty={skipped_empty} skipped_service={skipped_service} "
        f"skipped_other={skipped_other}"
    )
    print(f"target: {target}")
    print(f"bot_id={bot_id} user_id={user_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
