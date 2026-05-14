"""Principal-identity resolver: Telegram main chat + verified email address."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .conf import load_approvals_config


@dataclass(frozen=True)
class Principal:
    telegram_chat_id: str | None = None
    telegram_user_id: str | None = None
    email: str | None = None
    email_domain: str | None = None


def _read_principal_block(instance_dir: Path) -> dict[str, Any]:
    cfg_path = Path(instance_dir) / "ops" / "gateway.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        try:
            from gateway.config import _parse_simple_yaml  # type: ignore

            data = _parse_simple_yaml(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    if not isinstance(data, dict):
        return {}
    block = data.get("principal")
    return block if isinstance(block, dict) else {}


def _env_value(instance_dir: Path, key: str) -> str:
    """Mirror gateway.config.env_value without the os.environ-first leak."""
    env_path = Path(instance_dir) / ".env"
    if env_path.exists():
        try:
            for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    val = v.strip()
                    if val.startswith(("'", '"')) and val.endswith(("'", '"')) and len(val) >= 2:
                        val = val[1:-1]
                    return val
        except OSError:
            pass
    return os.environ.get(key, "")


def load_principal(instance_dir: Path) -> Principal:
    """Resolve the principal from `ops/gateway.yaml` then `.env`/env vars."""
    block = _read_principal_block(instance_dir)

    chat_id = _coerce_str(block.get("telegram_chat_id")) or _env_value(
        instance_dir, "TELEGRAM_CHAT_ID"
    )
    user_id = _coerce_str(block.get("telegram_user_id")) or chat_id or _env_value(
        instance_dir, "TELEGRAM_USER_ID"
    )
    email = _coerce_str(block.get("email")) or _env_value(instance_dir, "PRINCIPAL_EMAIL")
    domain_raw = _coerce_str(block.get("email_domain"))
    if not domain_raw and email and "@" in email:
        domain_raw = email.rsplit("@", 1)[1]

    return Principal(
        telegram_chat_id=chat_id or None,
        telegram_user_id=user_id or None,
        email=email or None,
        email_domain=(domain_raw or None),
    )


def main_chat_id(instance_dir: Path) -> str | None:
    """Resolve the principal's DM chat id. Returns None on failure."""
    principal = load_principal(instance_dir)
    return principal.telegram_chat_id


def principal_email(instance_dir: Path) -> str | None:
    """Resolve the principal's verified email. Returns None on failure."""
    return load_principal(instance_dir).email


def require_telegram(instance_dir: Path) -> bool:
    """Operator preference: refuse to enqueue when Telegram delivery unavailable."""
    cfg = load_approvals_config(instance_dir)
    return cfg.require_telegram


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    return str(value).strip()
