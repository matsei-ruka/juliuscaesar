"""Gateway configuration and safe .env loading."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SUPPORTED_BRAINS = ("claude", "codex", "opencode", "gemini", "aider")
SUPPORTED_CHANNELS = ("telegram", "slack", "discord", "voice", "jc-events", "cron")
REJECTED_CHANNELS = {"web": "web channel removed in 0.3.0; use `jc gateway enqueue` for local testing"}


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
    # Per-channel extras consumed by the channel implementations.
    watch_dir: str | None = None
    poll_interval_seconds: int | None = None
    paired_with: str | None = None
    asr_provider: str | None = None
    tts_provider: str | None = None


@dataclass(frozen=True)
class TriageConfig:
    backend: str = "none"
    confidence_threshold: float = 0.7
    fallback_brain: str = "claude:sonnet-4-6"
    cache_ttl_seconds: int = 30
    sticky_idle_seconds: int = 0
    routing: dict[str, str] = field(default_factory=dict)
    ollama_model: str = "phi3:mini"
    ollama_host: str = "http://localhost:11434"
    ollama_timeout_seconds: int = 5
    openrouter_model: str = "meta-llama/llama-3.1-8b-instruct"
    openrouter_api_key_env: str = "OPENROUTER_API_KEY"
    openrouter_timeout_seconds: int = 5
    claude_triage_screen: str = "jc-triage"
    claude_triage_model: str = "claude-haiku-4-5"
    claude_triage_port: int = 9876


@dataclass(frozen=True)
class BrainOverrideConfig:
    bin: str | None = None
    sandbox: str | None = None
    yolo: bool | None = None
    timeout_seconds: int | None = None
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReliabilityConfig:
    max_queue_depth: int = 100
    log_max_bytes: int = 50 * 1024 * 1024
    log_backups: int = 5
    backoff_seconds: tuple[int, ...] = (10, 60, 300)


@dataclass(frozen=True)
class GatewayConfig:
    default_brain: str = "claude"
    default_model: str | None = None
    poll_interval_seconds: float = 1.0
    lease_seconds: int = 300
    max_retries: int = 3
    adapter_timeout_seconds: int = 300
    channels: dict[str, ChannelConfig] = field(default_factory=dict)
    triage: TriageConfig = field(default_factory=TriageConfig)
    brains: dict[str, BrainOverrideConfig] = field(default_factory=dict)
    reliability: ReliabilityConfig = field(default_factory=ReliabilityConfig)

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
        "discord": ChannelConfig(
            enabled=False,
            bot_token_env="DISCORD_BOT_TOKEN",
        ),
        "voice": ChannelConfig(
            enabled=False,
            paired_with="telegram",
            asr_provider="dashscope",
            tts_provider="dashscope",
        ),
        "jc-events": ChannelConfig(
            enabled=True,
            watch_dir="state/events",
            poll_interval_seconds=2,
        ),
        "cron": ChannelConfig(
            enabled=True,
            watch_dir="state/cron",
            poll_interval_seconds=2,
        ),
    }
)


def config_path(instance_dir: Path) -> Path:
    return instance_dir / "ops" / "gateway.yaml"


class ConfigError(ValueError):
    """Raised on configuration errors that the user must fix."""


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


def _normalize_channel_key(name: str) -> str:
    return name.replace("_", "-").lower()


def _load_channel(name: str, raw: dict[str, Any], defaults: ChannelConfig) -> ChannelConfig:
    brain_value = raw.get("brain")
    brain = str(brain_value) if brain_value in SUPPORTED_BRAINS else None

    def _opt_str(key: str, default: str | None) -> str | None:
        v = raw.get(key, default)
        return str(v) if v is not None else None

    def _opt_int(key: str, default: int | None) -> int | None:
        v = raw.get(key, default)
        return int(v) if v is not None else None

    return ChannelConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        token_env=str(raw.get("token_env") or defaults.token_env),
        app_token_env=str(raw.get("app_token_env") or defaults.app_token_env),
        bot_token_env=str(raw.get("bot_token_env") or defaults.bot_token_env),
        chat_ids=_tuple_str(raw.get("chat_ids", defaults.chat_ids)),
        brain=brain,
        model=str(raw["model"]) if raw.get("model") is not None else defaults.model,
        timeout_seconds=int(raw.get("timeout_seconds") or defaults.timeout_seconds),
        watch_dir=_opt_str("watch_dir", defaults.watch_dir),
        poll_interval_seconds=_opt_int("poll_interval_seconds", defaults.poll_interval_seconds),
        paired_with=_opt_str("paired_with", defaults.paired_with),
        asr_provider=_opt_str("asr_provider", defaults.asr_provider),
        tts_provider=_opt_str("tts_provider", defaults.tts_provider),
    )


def _load_triage(data: dict[str, Any]) -> TriageConfig:
    raw = data.get("triage")
    backend = "none"
    if isinstance(raw, str):
        backend = raw
        raw = {}
    elif isinstance(raw, dict):
        backend = str(raw.get("backend") or raw.get("mode") or backend)
    else:
        raw = {}

    routing_raw = data.get("triage_routing") or raw.get("routing") or {}
    routing: dict[str, str] = {}
    if isinstance(routing_raw, dict):
        for key, value in routing_raw.items():
            if isinstance(value, str):
                routing[str(key)] = value

    def _opt(key: str, default: Any) -> Any:
        return data.get(key, raw.get(key, default))

    return TriageConfig(
        backend=str(backend or "none"),
        confidence_threshold=float(_opt("triage_confidence_threshold", 0.7)),
        fallback_brain=str(_opt("default_fallback_brain", "claude:sonnet-4-6")),
        cache_ttl_seconds=int(_opt("triage_cache_ttl_seconds", 30)),
        sticky_idle_seconds=int(_opt("sticky_brain_idle_timeout_seconds", 0)),
        routing=routing,
        ollama_model=str(_opt("ollama_model", "phi3:mini")),
        ollama_host=str(_opt("ollama_host", "http://localhost:11434")),
        ollama_timeout_seconds=int(_opt("ollama_timeout_seconds", 5)),
        openrouter_model=str(_opt("openrouter_model", "meta-llama/llama-3.1-8b-instruct")),
        openrouter_api_key_env=str(_opt("openrouter_api_key_env", "OPENROUTER_API_KEY")),
        openrouter_timeout_seconds=int(_opt("openrouter_timeout_seconds", 5)),
        claude_triage_screen=str(_opt("claude_triage_screen", "jc-triage")),
        claude_triage_model=str(_opt("claude_triage_model", "claude-haiku-4-5")),
        claude_triage_port=int(_opt("claude_triage_port", 9876)),
    )


def _load_brains(data: dict[str, Any]) -> dict[str, BrainOverrideConfig]:
    raw = data.get("brains")
    out: dict[str, BrainOverrideConfig] = {}
    if not isinstance(raw, dict):
        return out
    for name, body in raw.items():
        if not isinstance(body, dict):
            continue
        out[str(name)] = BrainOverrideConfig(
            bin=str(body["bin"]) if body.get("bin") is not None else None,
            sandbox=str(body["sandbox"]) if body.get("sandbox") is not None else None,
            yolo=bool(body["yolo"]) if body.get("yolo") is not None else None,
            timeout_seconds=int(body["timeout_seconds"]) if body.get("timeout_seconds") is not None else None,
            extra_args=tuple(str(arg) for arg in (body.get("extra_args") or [])),
        )
    return out


def _load_reliability(data: dict[str, Any]) -> ReliabilityConfig:
    raw = data.get("reliability") if isinstance(data.get("reliability"), dict) else {}
    backoff = raw.get("backoff_seconds") or data.get("event_retry_backoff_seconds")
    if isinstance(backoff, (list, tuple)) and backoff:
        try:
            backoff_tuple = tuple(int(v) for v in backoff)
        except (TypeError, ValueError):
            backoff_tuple = (10, 60, 300)
    else:
        backoff_tuple = (10, 60, 300)
    return ReliabilityConfig(
        max_queue_depth=int(raw.get("max_queue_depth") or data.get("max_queue_depth") or 100),
        log_max_bytes=int(raw.get("log_max_bytes") or 50 * 1024 * 1024),
        log_backups=int(raw.get("log_backups") or 5),
        backoff_seconds=backoff_tuple,
    )


def load_config(instance_dir: Path) -> GatewayConfig:
    data = _load_raw(config_path(instance_dir))
    gateway = data.get("gateway", {}) if isinstance(data.get("gateway"), dict) else {}
    channels_raw = data.get("channels", {}) if isinstance(data.get("channels"), dict) else {}

    default_brain = str(data.get("default_brain") or DEFAULT_CONFIG.default_brain)
    if default_brain not in SUPPORTED_BRAINS:
        default_brain = DEFAULT_CONFIG.default_brain

    channels: dict[str, ChannelConfig] = {}
    for name, defaults in DEFAULT_CONFIG.channels.items():
        # Allow both `jc-events` and `jc_events` style keys.
        raw = channels_raw.get(name)
        if raw is None:
            raw = channels_raw.get(name.replace("-", "_"))
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        channels[name] = _load_channel(name, raw, defaults)

    # Reject removed channel keys early with a helpful message.
    for raw_key in channels_raw.keys():
        normalized = _normalize_channel_key(str(raw_key))
        if normalized in REJECTED_CHANNELS:
            raise ConfigError(
                f"channels.{raw_key}: {REJECTED_CHANNELS[normalized]}"
            )
        if normalized not in SUPPORTED_CHANNELS and normalized not in {"web"}:
            # Unknown channels are tolerated (forward-compat) but logged via raise.
            # We accept them silently for now to avoid breaking older configs.
            pass

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
        triage=_load_triage(data),
        brains=_load_brains(data),
        reliability=_load_reliability(data),
    )


def render_default_config(
    *,
    default_brain: str = "claude",
    telegram_enabled: bool = False,
    telegram_chat_id: str = "",
    slack_enabled: bool = False,
    discord_enabled: bool = False,
    triage_backend: str = "none",
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
triage: {triage_backend}
triage_confidence_threshold: 0.7
default_fallback_brain: claude:sonnet-4-6
sticky_brain_idle_timeout_seconds: 0
triage_routing:
  smalltalk: claude:haiku-4-5
  quick: claude:sonnet-4-6
  analysis: claude:opus-4-7-1m
  code: claude:sonnet-4-6
  image: claude:sonnet-4-6
  voice: claude:sonnet-4-6
  system: claude:haiku-4-5
channels:
  telegram:
    enabled: {str(telegram_enabled).lower()}
    token_env: TELEGRAM_BOT_TOKEN
    chat_ids: {telegram_chats}
  slack:
    enabled: {str(slack_enabled).lower()}
    app_token_env: SLACK_APP_TOKEN
    bot_token_env: SLACK_BOT_TOKEN
  discord:
    enabled: {str(discord_enabled).lower()}
    bot_token_env: DISCORD_BOT_TOKEN
  voice:
    enabled: false
    paired_with: telegram
    asr_provider: dashscope
    tts_provider: dashscope
  jc-events:
    enabled: true
    watch_dir: state/events
    poll_interval_seconds: 2
  cron:
    enabled: true
    watch_dir: state/cron
    poll_interval_seconds: 2
"""
