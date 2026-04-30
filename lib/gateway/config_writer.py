"""Atomic config-file writers for the gateway.

The Telegram sender-approval flow mutates `ops/gateway.yaml` and `.env`
in response to operator inline-button taps. Both files are user-owned
artifacts (under version control + hand-edited), so writes must be:

  - **Atomic** — a crash mid-write must never leave the file half-baked.
    Pattern: write to `<path>.tmp`, then `os.replace`. POSIX guarantees
    `replace` is atomic on the same filesystem.
  - **Minimally destructive** — for `.env` we splice a single line in
    place and leave every other line (comments, ordering) untouched.
    For yaml we round-trip through PyYAML when available, falling back
    to a hand-rolled emitter that matches the layout of
    `render_default_config`. Comments are not preserved on yaml writes;
    operator edits should land outside the auto-managed sections.
  - **Idempotent** — adding an already-present chat_id is a no-op
    (no file write, mtime unchanged). Same for removal of a missing id.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

from .config import config_path, parse_env_file

ENV_CHAT_IDS_VAR = "TELEGRAM_CHAT_IDS"


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically via tmp + os.replace.

    The temp file lives next to the destination so the rename is
    same-filesystem (otherwise `os.replace` falls back to a copy and
    loses atomicity on POSIX).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ──────────────────────────── .env ────────────────────────────


def _parse_chat_id_list(raw: str) -> list[str]:
    """Split a `TELEGRAM_CHAT_IDS` value into stripped, non-empty entries."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def env_chat_ids(instance_dir: Path) -> tuple[str, ...]:
    """Return the parsed `TELEGRAM_CHAT_IDS` tuple from `.env`.

    Empty tuple when the variable is absent. No process-env lookup —
    auth is sourced from disk so external edits hot-reload.
    """
    raw = parse_env_file(instance_dir / ".env").get(ENV_CHAT_IDS_VAR, "")
    return tuple(_parse_chat_id_list(raw))


def _format_env_value(value: str) -> str:
    """Single-quote a `.env` value to keep shells happy.

    Comma-separated chat_id lists are common; shells split on whitespace
    not commas, but we still quote for safety against future edits.
    """
    if "'" in value:
        return f'"{value}"'
    return f"'{value}'"


def update_env_chat_ids(
    instance_dir: Path,
    *,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
) -> bool:
    """Mutate `TELEGRAM_CHAT_IDS=` in `.env`. Returns True iff written.

    Adds/removes idempotently. Preserves all other lines verbatim.
    Creates `.env` if it doesn't exist (only when `add` is non-empty).
    """
    add_set = {str(x) for x in add if str(x)}
    remove_set = {str(x) for x in remove if str(x)}
    env_path = instance_dir / ".env"
    original_text = ""
    lines: list[str] = []
    if env_path.exists():
        original_text = env_path.read_text(encoding="utf-8", errors="replace")
        lines = original_text.splitlines()

    line_idx: int | None = None
    current: list[str] = []
    pattern = re.compile(rf"^\s*{re.escape(ENV_CHAT_IDS_VAR)}\s*=(.*)$")
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m:
            line_idx = i
            current = _parse_chat_id_list(m.group(1).strip().strip("'\""))
            break

    new_ids = list(current)
    seen = set(new_ids)
    for cid in add_set:
        if cid not in seen:
            new_ids.append(cid)
            seen.add(cid)
    new_ids = [cid for cid in new_ids if cid not in remove_set]

    if new_ids == current and line_idx is not None:
        return False
    if not new_ids and line_idx is None:
        return False

    new_value = ",".join(new_ids)
    new_line = f"{ENV_CHAT_IDS_VAR}={_format_env_value(new_value)}"
    if line_idx is not None:
        lines[line_idx] = new_line
    else:
        lines.append(new_line)
    new_text = "\n".join(lines)
    if original_text and not original_text.endswith("\n"):
        # Preserve trailing-newline state from the original file.
        pass
    if not new_text.endswith("\n"):
        new_text += "\n"
    if new_text == original_text:
        return False
    atomic_write_text(env_path, new_text)
    return True


# ─────────────────────────── gateway.yaml ───────────────────────────


def _load_yaml_text(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        from .config import _parse_simple_yaml

        return _parse_simple_yaml(text)


def _dump_yaml(data: dict[str, Any]) -> str:
    """Serialize a gateway-config dict in our canonical layout.

    Uses PyYAML when available with flow-style chat-id lists so diffs
    stay one-line. Falls back to a minimal hand-rolled emitter that
    matches `render_default_config`.
    """
    try:
        import yaml  # type: ignore

        class _Dumper(yaml.SafeDumper):
            pass

        def _list_repr(dumper: Any, data: Any) -> Any:
            return dumper.represent_sequence(
                "tag:yaml.org,2002:seq", data, flow_style=True
            )

        _Dumper.add_representer(list, _list_repr)
        return yaml.dump(
            data,
            Dumper=_Dumper,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
    except Exception:
        return _hand_dump(data)


def _scalar_repr(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if (
        not text
        or text.startswith(("[", "{", "*", "&", "!", "|", ">", "%", "@", "`"))
        or any(ch in text for ch in (":", "#", "\n", '"', "'"))
        or text.lower() in ("true", "false", "null", "yes", "no")
    ):
        return f'"{text}"'
    try:
        int(text)
        return f'"{text}"'
    except ValueError:
        pass
    return text


def _list_repr(value: list[Any]) -> str:
    return "[" + ", ".join(_scalar_repr(v) for v in value) + "]"


def _hand_dump(data: dict[str, Any], indent: int = 0) -> str:
    out: list[str] = []
    pad = "  " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            out.append(f"{pad}{key}:")
            if value:
                out.append(_hand_dump(value, indent + 1))
        elif isinstance(value, list):
            out.append(f"{pad}{key}: {_list_repr(value)}")
        else:
            out.append(f"{pad}{key}: {_scalar_repr(value)}")
    return "\n".join(out) + ("\n" if indent == 0 else "")


def _normalize_id_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v)]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1]
        return [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]
    return [text]


def update_gateway_yaml_chat_lists(
    instance_dir: Path,
    *,
    channel: str = "telegram",
    allow_add: Iterable[str] = (),
    allow_remove: Iterable[str] = (),
    block_add: Iterable[str] = (),
    block_remove: Iterable[str] = (),
) -> bool:
    """Mutate `channels.<channel>.chat_ids` and `blocked_chat_ids` in yaml.

    Idempotent set-mutation: returns True iff the file was rewritten.
    Preserves other top-level keys + sibling channels but does NOT
    preserve YAML comments (PyYAML round-trip limitation). Operator
    is expected to keep comments outside the auto-managed channel
    block, or use ruamel-yaml in a future PR if comment preservation
    becomes critical.
    """
    yaml_path = config_path(instance_dir)
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
    data = _load_yaml_text(text) if text else {}

    channels = data.setdefault("channels", {})
    if not isinstance(channels, dict):
        channels = {}
        data["channels"] = channels
    chan = channels.setdefault(channel, {})
    if not isinstance(chan, dict):
        chan = {}
        channels[channel] = chan

    cur_allow = _normalize_id_list(chan.get("chat_ids"))
    cur_block = _normalize_id_list(chan.get("blocked_chat_ids"))

    add_a = {str(x) for x in allow_add if str(x)}
    rem_a = {str(x) for x in allow_remove if str(x)}
    add_b = {str(x) for x in block_add if str(x)}
    rem_b = {str(x) for x in block_remove if str(x)}

    new_allow = _apply_set_mutation(cur_allow, add_a, rem_a)
    new_block = _apply_set_mutation(cur_block, add_b, rem_b)

    if new_allow == cur_allow and new_block == cur_block:
        return False

    chan["chat_ids"] = new_allow
    if new_block:
        chan["blocked_chat_ids"] = new_block
    elif "blocked_chat_ids" in chan:
        # Don't leave an empty list lying around — drop the key.
        del chan["blocked_chat_ids"]

    new_text = _dump_yaml(data)
    if new_text == text:
        return False
    atomic_write_text(yaml_path, new_text)
    return True


def _apply_set_mutation(
    current: list[str], add: set[str], remove: set[str]
) -> list[str]:
    out = list(current)
    seen = set(out)
    for cid in add:
        if cid not in seen:
            out.append(cid)
            seen.add(cid)
    return [cid for cid in out if cid not in remove]
