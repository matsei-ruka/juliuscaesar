"""Telegram slash command handlers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .base import LogFn
from .telegram_outbound import send_text


def parse_slash_command(text: str) -> tuple[str, list[str]] | None:
    """Parse slash command from text. Returns (command, args) or None.

    Examples:
      "/help" → ("help", [])
      "/models" → ("models", [])
      "/compact" → ("compact", [])
    """
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts = text.split()
    if not parts:
        return None
    command = parts[0][1:].lower()
    args = parts[1:] if len(parts) > 1 else []
    return (command, args)


def handle_slash_command(
    command: str,
    args: list[str],
    instance_dir: Path,
    token: str,
    meta: dict[str, Any],
    log: LogFn,
) -> bool:
    """Handle a slash command. Returns True if handled, False if unknown.

    Sends response directly via Telegram API, does not enqueue to brain.
    """
    handlers = {
        "help": _handle_help,
        "models": _handle_models,
        "compact": _handle_compact,
    }
    handler = handlers.get(command)
    if not handler:
        response = f"Unknown command: /{command}\n\nRun `/help` for available commands."
        try:
            send_text(
                instance_dir=instance_dir,
                token=token,
                response=response,
                meta=meta,
                log=log,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"telegram command response send failed: {exc}")
        return False
    try:
        handler(instance_dir, token, meta, log, *args)
    except Exception as exc:  # noqa: BLE001
        log(f"telegram command handler failed /{command}: {exc}")
        try:
            send_text(
                instance_dir=instance_dir,
                token=token,
                response=f"Command failed: {exc}",
                meta=meta,
                log=log,
            )
        except Exception:  # noqa: BLE001
            pass
    return True


def _handle_help(instance_dir: Path, token: str, meta: dict[str, Any], log: LogFn) -> None:
    """Handle /help command."""
    response = """/help — show this message
/models — display model routing table
/compact — request context compaction"""
    send_text(
        instance_dir=instance_dir,
        token=token,
        response=response,
        meta=meta,
        log=log,
    )


def _handle_models(instance_dir: Path, token: str, meta: dict[str, Any], log: LogFn) -> None:
    """Handle /models command — show triage routing."""
    gateway_config_path = instance_dir / "ops" / "gateway.yaml"
    if not gateway_config_path.exists():
        raise FileNotFoundError(f"No gateway config at {gateway_config_path}")

    with open(gateway_config_path) as f:
        config = yaml.safe_load(f) or {}

    default_brain = config.get("default_brain", "?")
    fallback_brain = config.get("default_fallback_brain", "?")
    sticky_timeout = config.get("sticky_brain_idle_timeout_seconds", "?")

    routing = config.get("triage_routing", {})
    lines = [
        f"Default brain: `{default_brain}`",
        f"Fallback brain: `{fallback_brain}`",
        f"Sticky timeout: {sticky_timeout}s",
        "",
        "Triage routing:",
    ]
    for task_type, model in sorted(routing.items()):
        lines.append(f"  `{task_type}` ↦ `{model}`")

    response = "\n".join(lines)
    send_text(
        instance_dir=instance_dir,
        token=token,
        response=response,
        meta=meta,
        log=log,
    )


def _handle_compact(instance_dir: Path, token: str, meta: dict[str, Any], log: LogFn) -> None:
    """Handle /compact command — request context compaction."""
    signals_dir = instance_dir / "state" / "signals"
    signals_dir.mkdir(parents=True, exist_ok=True)
    compact_signal = signals_dir / "compact"
    compact_signal.touch()
    log(f"telegram compact signal written to {compact_signal}")

    response = "✓ Compaction request queued. Next response will measure context."
    send_text(
        instance_dir=instance_dir,
        token=token,
        response=response,
        meta=meta,
        log=log,
    )
