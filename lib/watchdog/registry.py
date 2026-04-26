"""Load `ops/watchdog.yaml` into a list of `ChildSpec`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .child import ChildSpec, HealthSpec, RestartSpec


def registry_path(instance_dir: Path) -> Path:
    return instance_dir / "ops" / "watchdog.yaml"


def _parse_yaml(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return _parse_simple_yaml(text)


def _coerce_scalar(value: str) -> Any:
    value = value.strip()
    if value in ("true", "True", "yes"):
        return True
    if value in ("false", "False", "no"):
        return False
    if value in ("null", "None", "~"):
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_coerce_scalar(part.strip()) for part in inner.split(",")]
    if (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    ):
        return value[1:-1]
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _strip_comment(line: str) -> str:
    quote: str | None = None
    out: list[str] = []
    for ch in line:
        if ch in ("'", '"'):
            quote = None if quote == ch else ch if quote is None else quote
        if ch == "#" and quote is None:
            break
        out.append(ch)
    return "".join(out).rstrip()


def _next_content(lines: list[str], start: int) -> tuple[int, str] | None:
    for j in range(start, len(lines)):
        raw = _strip_comment(lines[j])
        if raw.strip():
            return len(raw) - len(raw.lstrip(" ")), raw.strip()
    return None


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Tiny YAML subset parser for the watchdog template.

    Supports nested mappings, inline lists, scalar lists, and lists of
    mappings. This keeps watchdog config stdlib-only when PyYAML is absent.
    """
    lines = text.splitlines()
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    for i, original in enumerate(lines):
        raw = _strip_comment(original)
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if line.startswith("- "):
            if not isinstance(parent, list):
                continue
            body = line[2:].strip()
            if ":" not in body:
                parent.append(_coerce_scalar(body))
                continue
            key, _, value = body.partition(":")
            key = key.strip()
            value = value.strip()
            item: dict[str, Any] = {}
            parent.append(item)
            stack.append((indent, item))
            if value:
                item[key] = _coerce_scalar(value)
            else:
                next_line = _next_content(lines, i + 1)
                child: list[Any] | dict[str, Any]
                child = [] if next_line and next_line[1].startswith("- ") else {}
                item[key] = child
                stack.append((indent + 2, child))
            continue

        if ":" not in line or not isinstance(parent, dict):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value:
            parent[key] = _coerce_scalar(value)
            continue
        next_line = _next_content(lines, i + 1)
        child = [] if next_line and next_line[1].startswith("- ") else {}
        parent[key] = child
        stack.append((indent, child))

    return root


def _to_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _to_int_tuple(value: Any, default: tuple[int, ...]) -> tuple[int, ...]:
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        try:
            return tuple(int(v) for v in value)
        except (TypeError, ValueError):
            return default
    try:
        return (int(value),)
    except (TypeError, ValueError):
        return default


def _int_or(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_health(raw: Any) -> HealthSpec:
    if not isinstance(raw, dict):
        return HealthSpec()
    return HealthSpec(
        pid_alive=bool(raw.get("pid_alive")),
        cwd_match=str(raw["cwd_match"]) if raw.get("cwd_match") else None,
        proc_match=str(raw["proc_match"]) if raw.get("proc_match") else None,
        heartbeat_file=str(raw["heartbeat_file"]) if raw.get("heartbeat_file") else None,
        heartbeat_max_age_seconds=_int_or(raw.get("heartbeat_max_age_seconds"), 30),
    )


def _build_restart(raw: Any) -> RestartSpec:
    if not isinstance(raw, dict):
        return RestartSpec()
    return RestartSpec(
        backoff=_to_int_tuple(raw.get("backoff"), (5, 10, 30, 60, 300)),
        max_in_window=_int_or(raw.get("max_in_window"), 5),
        window_seconds=_int_or(raw.get("window_seconds"), 600),
        start_grace_seconds=_int_or(raw.get("start_grace_seconds"), 15),
    )


def _build_child(raw: dict[str, Any]) -> ChildSpec | None:
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    type_ = str(raw.get("type") or "daemon").strip()
    extra: dict[str, Any] = {}
    for key in ("screen_name", "session_id"):
        if raw.get(key) is not None:
            extra[key] = raw[key]
    return ChildSpec(
        name=name,
        type=type_,
        enabled=bool(raw.get("enabled", True)),
        start=str(raw["start"]) if raw.get("start") else None,
        pidfile=str(raw["pidfile"]) if raw.get("pidfile") else None,
        screen_name=str(raw["screen_name"]) if raw.get("screen_name") else None,
        session_id=str(raw["session_id"]) if raw.get("session_id") else None,
        extra=extra,
        health=_build_health(raw.get("health")),
        restart=_build_restart(raw.get("restart")),
    )


def load_registry(instance_dir: Path) -> list[ChildSpec]:
    """Return every child defined in `ops/watchdog.yaml` (enabled or not)."""
    path = registry_path(instance_dir)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    data = _parse_yaml(text)
    children_raw = data.get("children")
    if not isinstance(children_raw, list):
        return []
    children: list[ChildSpec] = []
    for entry in children_raw:
        if not isinstance(entry, dict):
            continue
        spec = _build_child(entry)
        if spec is not None:
            children.append(spec)
    return children


def load_enabled(instance_dir: Path) -> list[ChildSpec]:
    return [c for c in load_registry(instance_dir) if c.enabled]


WATCHDOG_YAML_TEMPLATE = """# Watchdog v2 — generic process supervisor.
# Replaces ops/watchdog.conf on instances that opt in via `jc watchdog migrate`.
#
# Each child entry must declare `name` and `type`. Health probes and restart
# policy fields below are all optional with sensible defaults.

children:
  - name: jc-gateway
    type: daemon
    enabled: true
    start: jc-gateway --instance-dir $INSTANCE_DIR start
    pidfile: state/gateway/jc-gateway.pid
    health:
      pid_alive: true
      cwd_match: $INSTANCE_DIR
      heartbeat_file: state/gateway/heartbeat
      heartbeat_max_age_seconds: 30
    restart:
      backoff: [5, 10, 30, 60, 300]
      max_in_window: 5
      window_seconds: 600
      start_grace_seconds: 15

  # Legacy entry — only present on instances pre-0.3.0. Generates a deprecation
  # warning every tick and runs the original watchdog.sh main() path. Remove
  # this block once your instance no longer needs the screen+claude+plugin
  # session (the unified gateway covers Telegram + voice natively).
  #
  # - name: claude-session
  #   type: legacy-claude
  #   enabled: false
  #   screen_name: jc-instance
  #   session_id: ""
  #   health:
  #     cwd_match: $INSTANCE_DIR
  #     proc_match: "claude .*--channels plugin:telegram"
  #   restart:
  #     backoff: [5, 10, 30, 60, 300]
  #     max_in_window: 5
  #     window_seconds: 600
"""


def render_default_yaml() -> str:
    return WATCHDOG_YAML_TEMPLATE
