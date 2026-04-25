"""Gateway configuration and safe .env loading."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SUPPORTED_BRAINS = ("claude", "codex", "opencode", "gemini")


@dataclass(frozen=True)
class ChannelConfig:
    enabled: bool = False
    token_env: str = ""
    app_token_env: str = ""
    bot_token_env: str = ""
    chat_ids: tuple[str, ...] = ()
    brain: str | None = None
    model: str | None = None
    timeout_seconds: int = 25


@dataclass(frozen=True)
class GatewayConfig:
    default_brain: str = "claude"
    default_model: str | None = None
    poll_interval_seconds: float = 1.0
    lease_seconds: int = 300
    max_retries: int = 3
    adapter_timeout_seconds: int = 300
    channels: dict[str, ChannelConfig] = field(default_factory=dict)

    def channel(self, name: str) -> ChannelConfig:
        return self.channels.get(name, ChannelConfig())

    def brain_for(self, channel: str) -> tuple[str, str | None]:
        cfg = self.channel(channel)
        return cfg.brain or self.default_brain, cfg.model if cfg.model is not None else self.default_model


DEFAULT_CONFIG = GatewayConfig(
    channels={
        "telegram": ChannelConfig(
            enabled=False,
            token_env="TELEGRAM_BOT_TOKEN",
            chat_ids=(),
            timeout_seconds=25,
        ),
        "slack": ChannelConfig(
            enabled=False,
            app_token_env="SLACK_APP_TOKEN",
            bot_token_env="SLACK_BOT_TOKEN",
        ),
    }
)


def config_path(instance_dir: Path) -> Path:
    return instance_dir / "ops" / "gateway.yaml"


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError:
            parsed = [value.strip().strip("'\"")]
        values[key] = parsed[0] if parsed else ""
    return values


def env_value(instance_dir: Path, name: str) -> str:
    return os.environ.get(name) or parse_env_file(instance_dir / ".env").get(name, "")


def _coerce_scalar(value: str) -> Any:
    value = value.strip()
    if value in ("true", "True", "yes", "YES", "Yes"):
        return True
    if value in ("false", "False", "no", "NO", "No"):
        return False
    if value in ("null", "None", "~"):
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [str(_coerce_scalar(part.strip())).strip() for part in inner.split(",")]
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


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _coerce_scalar(value)
    return root


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return _parse_simple_yaml(text)


def _tuple_str(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if str(v))
    text = str(value)
    return (text,) if text else ()


def load_config(instance_dir: Path) -> GatewayConfig:
    data = _load_raw(config_path(instance_dir))
    gateway = data.get("gateway", {}) if isinstance(data.get("gateway"), dict) else {}
    channels_raw = data.get("channels", {}) if isinstance(data.get("channels"), dict) else {}

    default_brain = str(data.get("default_brain") or DEFAULT_CONFIG.default_brain)
    if default_brain not in SUPPORTED_BRAINS:
        default_brain = DEFAULT_CONFIG.default_brain

    channels: dict[str, ChannelConfig] = {}
    for name, defaults in DEFAULT_CONFIG.channels.items():
        raw = channels_raw.get(name, {}) if isinstance(channels_raw.get(name), dict) else {}
        brain = raw.get("brain")
        brain = str(brain) if brain in SUPPORTED_BRAINS else None
        channels[name] = ChannelConfig(
            enabled=bool(raw.get("enabled", defaults.enabled)),
            token_env=str(raw.get("token_env") or defaults.token_env),
            app_token_env=str(raw.get("app_token_env") or defaults.app_token_env),
            bot_token_env=str(raw.get("bot_token_env") or defaults.bot_token_env),
            chat_ids=_tuple_str(raw.get("chat_ids", defaults.chat_ids)),
            brain=brain,
            model=str(raw["model"]) if raw.get("model") is not None else defaults.model,
            timeout_seconds=int(raw.get("timeout_seconds") or defaults.timeout_seconds),
        )

    return GatewayConfig(
        default_brain=default_brain,
        default_model=str(data["default_model"])
        if data.get("default_model") is not None and not isinstance(data.get("default_model"), dict)
        else None,
        poll_interval_seconds=float(gateway.get("poll_interval_seconds") or DEFAULT_CONFIG.poll_interval_seconds),
        lease_seconds=int(gateway.get("lease_seconds") or DEFAULT_CONFIG.lease_seconds),
        max_retries=int(gateway.get("max_retries") or DEFAULT_CONFIG.max_retries),
        adapter_timeout_seconds=int(
            gateway.get("adapter_timeout_seconds") or DEFAULT_CONFIG.adapter_timeout_seconds
        ),
        channels=channels,
    )


def render_default_config(
    *,
    default_brain: str = "claude",
    telegram_enabled: bool = False,
    telegram_chat_id: str = "",
    slack_enabled: bool = False,
) -> str:
    telegram_chats = f"[{telegram_chat_id}]" if telegram_chat_id else "[]"
    return f"""# JuliusCaesar gateway runtime config. Secrets live in .env.
default_brain: {default_brain}
default_model: null
gateway:
  poll_interval_seconds: 1
  lease_seconds: 300
  max_retries: 3
  adapter_timeout_seconds: 300
channels:
  telegram:
    enabled: {str(telegram_enabled).lower()}
    token_env: TELEGRAM_BOT_TOKEN
    chat_ids: {telegram_chats}
  slack:
    enabled: {str(slack_enabled).lower()}
    app_token_env: SLACK_APP_TOKEN
    bot_token_env: SLACK_BOT_TOKEN
"""
