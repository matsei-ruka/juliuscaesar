"""Gateway configuration and safe .env loading."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SUPPORTED_BRAINS = ("claude", "codex", "codex_api", "opencode", "gemini", "aider")
SUPPORTED_CHANNELS = ("telegram", "slack", "discord", "voice", "jc-events", "cron", "email")
SUPPORTED_TRIAGE_BACKENDS = (
    "none",
    "always",
    "ollama",
    "openrouter",
    "claude-channel",
    "codex_api",
)
REJECTED_CHANNELS = {"web": "web channel removed in 0.3.0; use `jc gateway enqueue` for local testing"}


@dataclass(frozen=True)
class ChannelConfig:
    enabled: bool = False
    token_env: str = ""
    app_token_env: str = ""
    bot_token_env: str = ""
    chat_ids: tuple[str, ...] = ()
    # Telegram only: chats explicitly rejected via the inline-button
    # approval flow. Consulted before `chat_ids`. Editable from yaml.
    blocked_chat_ids: tuple[str, ...] = ()
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
class CodexAuthConfig:
    auth_file: str = "~/.codex/auth.json"
    client_id_override: str | None = None
    refresh_skew_seconds: int = 300


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
    codex_auth: CodexAuthConfig = field(default_factory=CodexAuthConfig)

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
        "email": ChannelConfig(
            enabled=False,
        ),
    }
)


def config_path(instance_dir: Path) -> Path:
    return instance_dir / "ops" / "gateway.yaml"


class ConfigError(ValueError):
    """Raised on configuration errors that the user must fix."""


_ENV_CACHE: dict[Path, tuple[float | None, dict[str, str]]] = {}


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


def clear_env_cache() -> None:
    _ENV_CACHE.clear()


def env_values(instance_dir: Path) -> dict[str, str]:
    path = instance_dir / ".env"
    try:
        mtime: float | None = path.stat().st_mtime
    except FileNotFoundError:
        mtime = None
    cached = _ENV_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return dict(cached[1])
    values = parse_env_file(path)
    _ENV_CACHE[path] = (mtime, values)
    return dict(values)


def env_value(instance_dir: Path, name: str) -> str:
    return os.environ.get(name) or env_values(instance_dir).get(name, "")


def redact_value(name: str, value: str) -> str:
    if not value:
        return ""
    marker = name.upper()
    if any(part in marker for part in ("TOKEN", "SECRET", "KEY", "PASSWORD")):
        return "***"
    if len(value) > 24 and any(part in marker for part in ("AUTH", "COOKIE")):
        return value[:4] + "..." + value[-4:]
    return value


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


def _is_int_like(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _is_number_like(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _validate_positive_int(errors: list[str], path: str, value: Any) -> None:
    if not _is_int_like(value) or int(value) <= 0:
        errors.append(f"{path}: must be a positive integer")


def _validate_nonnegative_int(errors: list[str], path: str, value: Any) -> None:
    if not _is_int_like(value) or int(value) < 0:
        errors.append(f"{path}: must be a non-negative integer")


def _validate_brain_spec(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        errors.append(f"{path}: must be a brain name")
        return
    brain = value.partition(":")[0]
    if brain not in SUPPORTED_BRAINS:
        errors.append(f"{path}: unsupported brain {brain!r}")


def _validate_raw_config(data: dict[str, Any]) -> None:
    errors: list[str] = []
    allowed_top = {
        "default_brain",
        "default_model",
        "gateway",
        "triage",
        "triage_confidence_threshold",
        "default_fallback_brain",
        "sticky_brain_idle_timeout_seconds",
        "triage_routing",
        "channels",
        "brains",
        "reliability",
        "max_queue_depth",
        "event_retry_backoff_seconds",
        "ollama_model",
        "ollama_host",
        "ollama_timeout_seconds",
        "openrouter_model",
        "openrouter_api_key_env",
        "openrouter_timeout_seconds",
        "claude_triage_screen",
        "claude_triage_model",
        "claude_triage_port",
        "company",
        "codex_auth",
    }
    for key in data:
        if key not in allowed_top:
            errors.append(f"{key}: unknown top-level key")

    if data.get("default_brain") is not None:
        _validate_brain_spec(errors, "default_brain", data["default_brain"])
    if data.get("default_fallback_brain") is not None:
        _validate_brain_spec(errors, "default_fallback_brain", data["default_fallback_brain"])

    gateway = data.get("gateway")
    if gateway is not None:
        if not isinstance(gateway, dict):
            errors.append("gateway: must be a mapping")
        else:
            for key in gateway:
                if key not in {"poll_interval_seconds", "lease_seconds", "max_retries", "adapter_timeout_seconds"}:
                    errors.append(f"gateway.{key}: unknown field")
            if gateway.get("poll_interval_seconds") is not None and (
                not _is_number_like(gateway["poll_interval_seconds"])
                or float(gateway["poll_interval_seconds"]) <= 0
            ):
                errors.append("gateway.poll_interval_seconds: must be a positive number")
            for key in ("lease_seconds", "adapter_timeout_seconds"):
                if gateway.get(key) is not None:
                    _validate_positive_int(errors, f"gateway.{key}", gateway[key])
            if gateway.get("max_retries") is not None:
                _validate_nonnegative_int(errors, "gateway.max_retries", gateway["max_retries"])

    triage_raw = data.get("triage")
    if isinstance(triage_raw, dict):
        backend = str(triage_raw.get("backend") or triage_raw.get("mode") or "none")
        for key in triage_raw:
            if key not in {
                "backend",
                "mode",
                "routing",
                "triage_confidence_threshold",
                "default_fallback_brain",
                "triage_cache_ttl_seconds",
                "sticky_brain_idle_timeout_seconds",
                "ollama_model",
                "ollama_host",
                "ollama_timeout_seconds",
                "openrouter_model",
                "openrouter_api_key_env",
                "openrouter_timeout_seconds",
                "claude_triage_screen",
                "claude_triage_model",
                "claude_triage_port",
            }:
                errors.append(f"triage.{key}: unknown field")
    elif triage_raw is None:
        backend = "none"
    else:
        backend = str(triage_raw)
    if backend not in SUPPORTED_TRIAGE_BACKENDS:
        errors.append(f"triage.backend: unsupported backend {backend!r}")
    if data.get("triage_confidence_threshold") is not None:
        threshold = data["triage_confidence_threshold"]
        if not _is_number_like(threshold) or not 0 <= float(threshold) <= 1:
            errors.append("triage_confidence_threshold: must be between 0 and 1")
    if data.get("sticky_brain_idle_timeout_seconds") is not None:
        _validate_nonnegative_int(
            errors,
            "sticky_brain_idle_timeout_seconds",
            data["sticky_brain_idle_timeout_seconds"],
        )
    if isinstance(data.get("triage_routing"), dict):
        for key, value in data["triage_routing"].items():
            _validate_brain_spec(errors, f"triage_routing.{key}", value)

    channels_raw = data.get("channels")
    if channels_raw is not None:
        if not isinstance(channels_raw, dict):
            errors.append("channels: must be a mapping")
        else:
            channel_fields = {
                "enabled",
                "token_env",
                "app_token_env",
                "bot_token_env",
                "chat_ids",
                "blocked_chat_ids",
                "brain",
                "model",
                "timeout_seconds",
                "watch_dir",
                "poll_interval_seconds",
                "paired_with",
                "asr_provider",
                "tts_provider",
                # Email channel: nested dicts validated lazily by the channel itself.
                "imap",
                "smtp",
                "senders",
                "state",
                "body_limit",
                "notify_on_unknown",
                "telegram_chat_id",
            }
            for raw_key, raw_value in channels_raw.items():
                normalized = _normalize_channel_key(str(raw_key))
                if normalized in REJECTED_CHANNELS:
                    errors.append(f"channels.{raw_key}: {REJECTED_CHANNELS[normalized]}")
                    continue
                if normalized not in SUPPORTED_CHANNELS:
                    errors.append(f"channels.{raw_key}: unknown channel")
                    continue
                if not isinstance(raw_value, dict):
                    errors.append(f"channels.{raw_key}: must be a mapping")
                    continue
                for key in raw_value:
                    if key not in channel_fields:
                        errors.append(f"channels.{raw_key}.{key}: unknown field")
                if raw_value.get("enabled") is not None and not isinstance(raw_value["enabled"], bool):
                    errors.append(f"channels.{raw_key}.enabled: must be boolean")
                if raw_value.get("brain") is not None:
                    _validate_brain_spec(errors, f"channels.{raw_key}.brain", raw_value["brain"])
                for key in ("timeout_seconds", "poll_interval_seconds"):
                    if raw_value.get(key) is not None:
                        _validate_positive_int(errors, f"channels.{raw_key}.{key}", raw_value[key])
                if raw_value.get("chat_ids") is not None and not isinstance(
                    raw_value["chat_ids"], (str, list, tuple)
                ):
                    errors.append(f"channels.{raw_key}.chat_ids: must be a string or list")
                if raw_value.get("blocked_chat_ids") is not None and not isinstance(
                    raw_value["blocked_chat_ids"], (str, list, tuple)
                ):
                    errors.append(
                        f"channels.{raw_key}.blocked_chat_ids: must be a string or list"
                    )
                if raw_value.get("paired_with") is not None:
                    paired = _normalize_channel_key(str(raw_value["paired_with"]))
                    if paired not in SUPPORTED_CHANNELS:
                        errors.append(f"channels.{raw_key}.paired_with: unknown channel {paired!r}")

    brains_raw = data.get("brains")
    if brains_raw is not None:
        if not isinstance(brains_raw, dict):
            errors.append("brains: must be a mapping")
        else:
            for name, body in brains_raw.items():
                if str(name) not in SUPPORTED_BRAINS:
                    errors.append(f"brains.{name}: unsupported brain")
                    continue
                if not isinstance(body, dict):
                    errors.append(f"brains.{name}: must be a mapping")
                    continue
                for key in body:
                    if key not in {"bin", "sandbox", "yolo", "timeout_seconds", "extra_args"}:
                        errors.append(f"brains.{name}.{key}: unknown field")
                if body.get("timeout_seconds") is not None:
                    _validate_positive_int(errors, f"brains.{name}.timeout_seconds", body["timeout_seconds"])
                if body.get("extra_args") is not None and not isinstance(body["extra_args"], (list, tuple)):
                    errors.append(f"brains.{name}.extra_args: must be a list")

    reliability = data.get("reliability")
    if reliability is not None:
        if not isinstance(reliability, dict):
            errors.append("reliability: must be a mapping")
        else:
            for key in reliability:
                if key not in {"max_queue_depth", "log_max_bytes", "log_backups", "backoff_seconds"}:
                    errors.append(f"reliability.{key}: unknown field")
            for key in ("max_queue_depth", "log_max_bytes"):
                if reliability.get(key) is not None:
                    _validate_positive_int(errors, f"reliability.{key}", reliability[key])
            if reliability.get("log_backups") is not None:
                _validate_nonnegative_int(errors, "reliability.log_backups", reliability["log_backups"])
            backoff = reliability.get("backoff_seconds")
            if backoff is not None:
                if not isinstance(backoff, (list, tuple)) or not backoff:
                    errors.append("reliability.backoff_seconds: must be a non-empty list")
                else:
                    for idx, item in enumerate(backoff):
                        _validate_positive_int(errors, f"reliability.backoff_seconds[{idx}]", item)

    company_raw = data.get("company")
    if company_raw is not None:
        if not isinstance(company_raw, dict):
            errors.append("company: must be a mapping")
        else:
            allowed_company = {
                "enabled",
                "redact_conversations",
                "exclude_channels",
                "exclude_users",
                "conversation_max_chars",
                "outbox_max_mb",
                "outbox_max_age_hours",
            }
            for key in company_raw:
                if key not in allowed_company:
                    errors.append(f"company.{key}: unknown field")
            for key in ("enabled", "redact_conversations"):
                value = company_raw.get(key)
                if value is not None and not isinstance(value, bool):
                    errors.append(f"company.{key}: must be boolean")
            for key in ("conversation_max_chars", "outbox_max_mb", "outbox_max_age_hours"):
                if company_raw.get(key) is not None:
                    _validate_positive_int(errors, f"company.{key}", company_raw[key])
            for key in ("exclude_channels", "exclude_users"):
                value = company_raw.get(key)
                if value is not None and not isinstance(value, (list, tuple)):
                    errors.append(f"company.{key}: must be a list")

    codex_auth_raw = data.get("codex_auth")
    if codex_auth_raw is not None:
        if not isinstance(codex_auth_raw, dict):
            errors.append("codex_auth: must be a mapping")
        else:
            allowed_codex_auth = {"auth_file", "client_id_override", "refresh_skew_seconds"}
            for key in codex_auth_raw:
                if key not in allowed_codex_auth:
                    errors.append(f"codex_auth.{key}: unknown field")
            if codex_auth_raw.get("auth_file") is not None and not isinstance(
                codex_auth_raw["auth_file"], str
            ):
                errors.append("codex_auth.auth_file: must be a string")
            cid = codex_auth_raw.get("client_id_override")
            if cid is not None and not isinstance(cid, str):
                errors.append("codex_auth.client_id_override: must be a string or null")
            if codex_auth_raw.get("refresh_skew_seconds") is not None:
                _validate_nonnegative_int(
                    errors,
                    "codex_auth.refresh_skew_seconds",
                    codex_auth_raw["refresh_skew_seconds"],
                )

    if errors:
        raise ConfigError("; ".join(errors))


def validate_config(instance_dir: Path) -> GatewayConfig:
    return load_config(instance_dir)


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
    brain_name = str(brain_value).partition(":")[0] if brain_value is not None else ""
    brain = brain_name if brain_name in SUPPORTED_BRAINS else None

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
        blocked_chat_ids=_tuple_str(
            raw.get("blocked_chat_ids", defaults.blocked_chat_ids)
        ),
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


def _load_codex_auth(data: dict[str, Any]) -> CodexAuthConfig:
    raw = data.get("codex_auth") if isinstance(data.get("codex_auth"), dict) else {}
    return CodexAuthConfig(
        auth_file=str(raw.get("auth_file") or DEFAULT_CONFIG.codex_auth.auth_file),
        client_id_override=(
            str(raw["client_id_override"])
            if raw.get("client_id_override") is not None
            else None
        ),
        refresh_skew_seconds=int(
            raw.get("refresh_skew_seconds")
            if raw.get("refresh_skew_seconds") is not None
            else DEFAULT_CONFIG.codex_auth.refresh_skew_seconds
        ),
    )


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


_CONFIG_CACHE: dict[Path, tuple[float | None, "GatewayConfig"]] = {}


def clear_config_cache() -> None:
    _CONFIG_CACHE.clear()


def load_config_cached(instance_dir: Path) -> "GatewayConfig":
    """Mtime-cached `load_config`. Reloads on `ops/gateway.yaml` change.

    Cheap to call on a hot path (poll loop). On unchanged file, returns
    a stable reference; on change, reparses + revalidates. Errors are
    propagated so a typo in yaml still fails loud.
    """
    path = config_path(instance_dir)
    try:
        mtime: float | None = path.stat().st_mtime
    except FileNotFoundError:
        mtime = None
    cached = _CONFIG_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    cfg = load_config(instance_dir)
    _CONFIG_CACHE[path] = (mtime, cfg)
    return cfg


def load_config(instance_dir: Path) -> GatewayConfig:
    data = _load_raw(config_path(instance_dir))
    _validate_raw_config(data)
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
        codex_auth=_load_codex_auth(data),
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
