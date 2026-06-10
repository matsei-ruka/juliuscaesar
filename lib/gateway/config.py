"""Gateway configuration and safe .env loading."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import brain_spec as _brain_spec
from .lifecycle.profiles import ProfileRegistry
from .lifecycle.routing import Reserves, Thresholds


SUPPORTED_BRAINS = ("claude", "codex", "codex_api", "opencode", "gemini", "aider", "pi", "grok")
SUPPORTED_UNSAFE_FALLBACK_BRAINS = (*SUPPORTED_BRAINS, "openrouter")
SUPPORTED_CHANNELS = ("telegram", "slack", "discord", "voice", "jc-events", "cron", "email", "company-inbox", "whatsapp")
SUPPORTED_TRIAGE_BACKENDS = (
    "none",
    "always",
    "ollama",
    "openrouter",
    "api_classifier",
    "claude-channel",
    "codex_api",
)
SUPPORTED_TRIAGE_PROTOCOLS = ("openai_compat", "anthropic")
REJECTED_CHANNELS = {"web": "web channel is unsupported; use `jc gateway enqueue` for local testing"}
CODEX_SANDBOX_VALUES = {"read-only", "workspace-write", "yolo", "danger", "danger-full-access"}
CODEX_YOLO_SANDBOX_VALUES = {"yolo", "danger", "danger-full-access"}
DEFAULT_TRIAGE_ROUTING = {
    "smalltalk": "claude:haiku-4-5",
    "quick": "claude:sonnet-4-6",
    "analysis": "claude:opus-4-7-1m",
    "code": "claude:sonnet-4-6",
    "image": "claude:sonnet-4-6",
    "voice": "claude:sonnet-4-6",
    "system": "claude:haiku-4-5",
}


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
    # company-inbox only: which task statuses to pull, and the per-tick cap.
    inbox_status_filter: tuple[str, ...] = ()
    max_new_per_tick: int | None = None
    # company-inbox only: emit company.task_closed when an injected task leaves
    # the active set (opt-in; needs the backend to honour in_progress/blocked in
    # the inbox query). Off → goal clear relies on the goal_cache TTL floor.
    emit_task_closed: bool = False


@dataclass(frozen=True)
class TriageConfig:
    backend: str = "none"
    confidence_threshold: float = 0.7
    fallback_brain: str = "claude:sonnet-4-6"
    unsafe_fallback_brain: str = ""
    unsafe_fallback_timeout_seconds: int = 60
    cache_ttl_seconds: int = 30
    sticky_idle_seconds: int = 0
    routing: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TRIAGE_ROUTING))
    backup: dict[str, str] = field(default_factory=dict)
    ollama_model: str = "phi3:mini"
    ollama_host: str = "http://localhost:11434"
    ollama_timeout_seconds: int = 5
    openrouter_model: str = "meta-llama/llama-3.1-8b-instruct"
    openrouter_api_key_env: str = "OPENROUTER_API_KEY"
    openrouter_timeout_seconds: int = 5
    protocol: str = "openai_compat"
    base_url: str = ""
    api_key_env: str = ""
    model: str = ""
    timeout_seconds: int = 5
    max_tokens: int | None = None
    claude_triage_screen: str = "jc-triage"
    claude_triage_model: str = "claude-haiku-4-5"
    claude_triage_port: int = 9876


@dataclass(frozen=True)
class ReplyFooterConfig:
    enabled: bool = False
    emoji: str = "⚙️"
    show_model: bool = True
    show_session: bool = True
    show_elapsed: bool = True
    session_chars: int = 8
    separator: str = " · "


@dataclass(frozen=True)
class ParallelClassifierConfig:
    backend: str = "openrouter"
    model: str = "deepseek/deepseek-chat"
    timeout_seconds: int = 3
    cache_ttl_seconds: int = 30


@dataclass(frozen=True)
class ActionsConfig:
    """Supervisor card action buttons (Stop / Background).

    Phase 1 gates everything behind ``enabled`` (default off). Cards continue
    to render without buttons when off, matching pre-feature behavior.
    See docs/specs/supervisor-card-actions.md.
    """

    enabled: bool = False
    stop_grace_seconds: int = 5
    # Phase 2: Background button.
    # Cap on concurrent backgrounded sessions per chat_id. Refused beyond.
    max_background_per_chat: int = 3
    # When true, mid-task Telegram sends from a backgrounded session are
    # buffered into the action entry and prepended to its completion card
    # rather than delivered immediately. Off → sends pass through normally.
    suppress_background_tool_messages: bool = True


@dataclass(frozen=True)
class ParallelConfig:
    """Per-conversation parallel-slot dispatch.

    Default `max_concurrent=1` reproduces today's strictly-serial behavior;
    all gateway code paths must short-circuit on N=1 to stay byte-identical
    with the pre-parallel-slots implementation (no classifier call, no slot
    suffixes anywhere). See docs/specs/parallel-slots.md.
    """

    max_concurrent: int = 1
    transcript_context_lines: int = 20
    classifier: ParallelClassifierConfig = field(default_factory=ParallelClassifierConfig)


@dataclass(frozen=True)
class BrainOverrideConfig:
    bin: str | None = None
    sandbox: str | None = None
    yolo: bool | None = None
    timeout_seconds: int | None = None
    extra_args: tuple[str, ...] = ()
    no_tools: bool | None = None
    thinking: str | None = None
    vision_model: str | None = None


@dataclass(frozen=True)
class ReliabilityConfig:
    max_queue_depth: int = 100
    log_max_bytes: int = 50 * 1024 * 1024
    log_backups: int = 5
    backoff_seconds: tuple[int, ...] = (10, 60, 300)
    coalesce_same_conversation: bool = False


@dataclass(frozen=True)
class PrincipalConfig:
    telegram_chat_id: str | None = None
    telegram_user_id: str | None = None
    email: str | None = None
    email_domain: str | None = None


@dataclass(frozen=True)
class CodexAuthConfig:
    auth_file: str = "~/.codex/auth.json"
    client_id_override: str | None = None
    refresh_skew_seconds: int = 300


ACCOUNTABILITIES_AUTHORITY_CHANNELS = ("telegram-primary", "email", "none")


@dataclass(frozen=True)
class AccountabilitiesConfig:
    enabled: bool = False
    authority_channel: str = "telegram-primary"
    enactment_token: str = "OK enact"
    authority_email_sender: str = ""


@dataclass(frozen=True)
class EntitiesConfig:
    enabled: bool = False
    migrate_legacy_people: bool = False


@dataclass(frozen=True)
class InterAgentProtocolConfig:
    enabled: bool = False
    authority_map_path: str = "memory/L1/authority-map.md"
    require_self_declaration: bool = True


ADAPTIVE_DISCOVERY_UNKNOWN_POSTURES = ("conservative",)
ADAPTIVE_DISCOVERY_AUTHORITY_ALIAS = "authority"


@dataclass(frozen=True)
class AdaptiveDiscoveryConfig:
    enabled: bool = False
    default_unknown_posture: str = "conservative"
    high_stakes_escalation_channel: str = ADAPTIVE_DISCOVERY_AUTHORITY_ALIAS


@dataclass(frozen=True)
class NativeCompactionConfig:
    enabled: bool = True
    fallback_to_rotation: bool = True


@dataclass(frozen=True)
class SessionLifecycleConfig:
    """§9 — context-aware session lifecycle.

    `model_profiles` is the raw operator override block; the resolved
    `ProfileRegistry` is built lazily via `registry()` so the dataclass stays
    hashable/frozen. Disabled by default — `enabled: false` preserves current
    behavior except that context-limit errors still stop generic retry
    amplification (§23).
    """

    enabled: bool = False
    thresholds: Thresholds = field(default_factory=Thresholds)
    reserves: Reserves = field(default_factory=Reserves)
    native_compaction: NativeCompactionConfig = field(default_factory=NativeCompactionConfig)
    model_profiles: tuple[tuple[str, Any], ...] = ()

    def registry(self) -> ProfileRegistry:
        raw = {key: value for key, value in self.model_profiles}
        return ProfileRegistry.from_config(raw or None)


@dataclass(frozen=True)
class CompactionNotifyConfig:
    """§18.1 — operator notification on compaction. Default on."""

    enabled: bool = True


@dataclass(frozen=True)
class GatewayConfig:
    default_brain: str = "claude"
    default_model: str | None = None
    pin_to_default_brain: bool = False
    poll_interval_seconds: float = 1.0
    lease_seconds: int = 300
    max_retries: int = 3
    adapter_timeout_seconds: int = 300
    timezone: str = "UTC"
    channels: dict[str, ChannelConfig] = field(default_factory=dict)
    triage: TriageConfig = field(default_factory=TriageConfig)
    reply_footer: ReplyFooterConfig = field(default_factory=ReplyFooterConfig)
    brains: dict[str, BrainOverrideConfig] = field(default_factory=dict)
    reliability: ReliabilityConfig = field(default_factory=ReliabilityConfig)
    codex_auth: CodexAuthConfig = field(default_factory=CodexAuthConfig)
    principal: PrincipalConfig = field(default_factory=PrincipalConfig)
    accountabilities: AccountabilitiesConfig = field(default_factory=AccountabilitiesConfig)
    entities: EntitiesConfig = field(default_factory=EntitiesConfig)
    inter_agent_protocol: InterAgentProtocolConfig = field(
        default_factory=InterAgentProtocolConfig
    )
    adaptive_discovery: AdaptiveDiscoveryConfig = field(
        default_factory=AdaptiveDiscoveryConfig
    )
    parallel: ParallelConfig = field(default_factory=ParallelConfig)
    actions: ActionsConfig = field(default_factory=ActionsConfig)
    session_lifecycle: SessionLifecycleConfig = field(default_factory=SessionLifecycleConfig)
    compaction_notify: CompactionNotifyConfig = field(default_factory=CompactionNotifyConfig)

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
        "company-inbox": ChannelConfig(
            enabled=False,
            poll_interval_seconds=10,
            max_new_per_tick=5,
            inbox_status_filter=("pending", "accepted"),
        ),
        "whatsapp": ChannelConfig(
            enabled=False,
        ),
    }
)


def config_path(instance_dir: Path) -> Path:
    return instance_dir / "ops" / "gateway.yaml"


class ConfigError(ValueError):
    """Raised on configuration errors that the user must fix."""


_ENV_CACHE: dict[Path, tuple[float | None, dict[str, str]]] = {}
_RESERVED_INSTANCE_ENV_KEYS = {
    "BASH_ENV",
    "CLAUDE_ARGS_EXTRA",
    "CONF_FILE",
    "ENV",
    "ENV_FILE",
    "HOME",
    "IFS",
    "INSTANCE_DIR",
    "LOG_FILE",
    "LOGNAME",
    "OLDPWD",
    "PATH",
    "PWD",
    "PYTHONEXECUTABLE",
    "PYTHONHOME",
    "PYTHONPATH",
    "RUNTIME_MODE",
    "SCREEN_NAME",
    "SESSION_ID",
    "SHELL",
    "STATE_FILE",
    "TMPDIR",
    "USER",
    "VIRTUAL_ENV",
}
_RESERVED_INSTANCE_ENV_PREFIXES = (
    "BASH_FUNC_",
    "CODEX_",
    "DYLD_",
    "GATEWAY_",
    "JC_",
    "LD_",
    "WORKER_",
)


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
        # Shell-style dotenvs prefix lines with `export ` — without this the
        # key fails the identifier regex and the entry is silently dropped,
        # which the parent-env fallback then resolves from a sibling shell
        # (formatting quirk → token-leak vector, audit G-P2).
        if key.startswith("export ") or key.startswith("export\t"):
            key = key[len("export") :].strip()
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


def is_instance_env_key_allowed(name: str) -> bool:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return False
    marker = name.upper()
    if marker in _RESERVED_INSTANCE_ENV_KEYS:
        return False
    return not any(marker.startswith(prefix) for prefix in _RESERVED_INSTANCE_ENV_PREFIXES)


def safe_instance_env_values(instance_dir: Path) -> dict[str, str]:
    return {
        key: value
        for key, value in env_values(instance_dir).items()
        if is_instance_env_key_allowed(key)
    }


# Token-class keys that must NEVER resolve from the parent process env: a
# sibling instance's shell exporting TELEGRAM_BOT_TOKEN is the cross-instance
# impersonation vector (409 conflicts + session bleed). For these, `.env` is
# the only source of truth — absent means empty, and the call sites log the
# missing-token condition loudly. Audit feature 8 (env allowlisting).
_SECRET_ENV_KEYS: frozenset[str] = frozenset({
    "TELEGRAM_BOT_TOKEN",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DASHSCOPE_API_KEY",
    "COMPANY_API_KEY",
    "MINIMAX_API_KEY",
})


def env_value(instance_dir: Path, name: str) -> str:
    values = env_values(instance_dir)
    if name in values and is_instance_env_key_allowed(name):
        return values[name]
    if name in _SECRET_ENV_KEYS:
        return ""
    return os.environ.get(name, "")


def merge_instance_env(
    instance_dir: Path,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update(safe_instance_env_values(instance_dir))
    return env


def apply_instance_env(instance_dir: Path) -> dict[str, str]:
    applied = safe_instance_env_values(instance_dir)
    os.environ.update(applied)
    return applied


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


def _validate_brain_spec(
    errors: list[str],
    path: str,
    value: Any,
    *,
    supported: tuple[str, ...] = SUPPORTED_BRAINS,
) -> None:
    if not isinstance(value, str) or not value:
        errors.append(f"{path}: must be a brain name")
        return
    brain = value.partition(":")[0]
    if brain not in supported:
        errors.append(f"{path}: unsupported brain {brain!r}")


def _validate_raw_config(data: dict[str, Any]) -> None:
    errors: list[str] = []
    allowed_top = {
        "default_brain",
        "default_model",
        "pin_to_default_brain",
        "gateway",
        "timezone",
        "triage",
        "triage_confidence_threshold",
        "default_fallback_brain",
        "triage_unsafe_fallback_brain",
        "triage_unsafe_fallback_timeout_seconds",
        "sticky_brain_idle_timeout_seconds",
        "triage_routing",
        "reply_footer",
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
        "triage_protocol",
        "triage_base_url",
        "triage_api_key_env",
        "triage_model",
        "triage_timeout_seconds",
        "triage_max_tokens",
        "claude_triage_screen",
        "claude_triage_model",
        "claude_triage_port",
        "company",
        "codex_auth",
        "principal",
        "accountabilities",
        "entities",
        "inter_agent_protocol",
        "adaptive_discovery",
        "voice",
        "triage_backup",
        "supervisor",
        "parallel",
        "actions",
        "session_lifecycle",
        "compaction_notify",
    }
    for key in data:
        if key not in allowed_top:
            errors.append(f"{key}: unknown top-level key")

    if data.get("default_brain") is not None:
        _validate_brain_spec(errors, "default_brain", data["default_brain"])
        db_raw = data["default_brain"]
        if (
            isinstance(db_raw, str)
            and ":" in db_raw
            and data.get("default_model") is not None
        ):
            errors.append(
                "default_brain: includes a model; also setting default_model "
                "is ambiguous (set only one)"
            )
    if data.get("default_fallback_brain") is not None:
        _validate_brain_spec(errors, "default_fallback_brain", data["default_fallback_brain"])
    if data.get("triage_unsafe_fallback_brain") is not None:
        _validate_brain_spec(
            errors,
            "triage_unsafe_fallback_brain",
            data["triage_unsafe_fallback_brain"],
            supported=SUPPORTED_UNSAFE_FALLBACK_BRAINS,
        )
    if data.get("pin_to_default_brain") is not None and not isinstance(
        data["pin_to_default_brain"], bool
    ):
        errors.append("pin_to_default_brain: must be boolean")

    timezone_raw = data.get("timezone")
    if timezone_raw is not None:
        if not isinstance(timezone_raw, str) or not timezone_raw.strip():
            errors.append("timezone: must be a non-empty IANA name string")
        else:
            try:
                ZoneInfo(timezone_raw.strip())
            except ZoneInfoNotFoundError:
                errors.append(f"timezone: unknown IANA zone {timezone_raw!r}")
            except Exception as exc:  # noqa: BLE001 — surface zoneinfo errors verbatim
                errors.append(f"timezone: invalid {timezone_raw!r} ({exc})")

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
                "triage_unsafe_fallback_brain",
                "triage_unsafe_fallback_timeout_seconds",
                "triage_cache_ttl_seconds",
                "sticky_brain_idle_timeout_seconds",
                "ollama_model",
                "ollama_host",
                "ollama_timeout_seconds",
                "openrouter_model",
                "openrouter_api_key_env",
                "openrouter_timeout_seconds",
                "triage_protocol",
                "protocol",
                "triage_base_url",
                "base_url",
                "triage_api_key_env",
                "api_key_env",
                "triage_model",
                "model",
                "triage_timeout_seconds",
                "timeout_seconds",
                "triage_max_tokens",
                "max_tokens",
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
    def _triage_opt(name: str, nested: str | None = None) -> Any:
        if data.get(name) is not None:
            return data.get(name)
        if isinstance(triage_raw, dict):
            return triage_raw.get(nested or name)
        return None

    triage_protocol = _triage_opt("triage_protocol", "protocol")
    triage_base_url = _triage_opt("triage_base_url", "base_url")
    triage_api_key_env = _triage_opt("triage_api_key_env", "api_key_env")
    triage_model = _triage_opt("triage_model", "model")
    triage_timeout = _triage_opt("triage_timeout_seconds", "timeout_seconds")
    triage_max_tokens = _triage_opt("triage_max_tokens", "max_tokens")
    unsafe_fallback_brain = _triage_opt("triage_unsafe_fallback_brain")
    unsafe_fallback_timeout = _triage_opt("triage_unsafe_fallback_timeout_seconds")
    if unsafe_fallback_brain is not None:
        _validate_brain_spec(
            errors,
            "triage_unsafe_fallback_brain",
            unsafe_fallback_brain,
            supported=SUPPORTED_UNSAFE_FALLBACK_BRAINS,
        )
    if unsafe_fallback_timeout is not None:
        _validate_positive_int(
            errors,
            "triage_unsafe_fallback_timeout_seconds",
            unsafe_fallback_timeout,
        )
    if triage_protocol is not None:
        if backend != "api_classifier":
            errors.append("triage_protocol: only valid when triage backend is api_classifier")
        elif str(triage_protocol) not in SUPPORTED_TRIAGE_PROTOCOLS:
            supported = ", ".join(SUPPORTED_TRIAGE_PROTOCOLS)
            errors.append(f"triage_protocol: unsupported protocol {triage_protocol!r} (supported: {supported})")
    if backend == "api_classifier":
        protocol = str(triage_protocol or "openai_compat")
        if protocol not in SUPPORTED_TRIAGE_PROTOCOLS:
            supported = ", ".join(SUPPORTED_TRIAGE_PROTOCOLS)
            errors.append(f"triage_protocol: unsupported protocol {protocol!r} (supported: {supported})")
        if not isinstance(triage_base_url, str) or not triage_base_url.strip():
            errors.append("triage_base_url: required when triage backend is api_classifier")
        elif not triage_base_url.startswith(("http://", "https://")):
            errors.append("triage_base_url: must start with http:// or https://")
        if not isinstance(triage_api_key_env, str) or not triage_api_key_env.strip():
            errors.append("triage_api_key_env: required when triage backend is api_classifier")
        if not isinstance(triage_model, str) or not triage_model.strip():
            errors.append("triage_model: required when triage backend is api_classifier")
        if triage_timeout is not None and (
            not _is_number_like(triage_timeout)
            or float(triage_timeout) <= 0
            or float(triage_timeout) > 60
        ):
            errors.append("triage_timeout_seconds: must be a positive number <= 60")
        if triage_max_tokens is not None and (
            not _is_int_like(triage_max_tokens)
            or int(triage_max_tokens) <= 0
            or int(triage_max_tokens) > 4096
        ):
            errors.append("triage_max_tokens: must be a positive integer <= 4096")
        if protocol == "anthropic" and triage_max_tokens is None:
            errors.append("triage_max_tokens: required when triage_protocol is anthropic")

    reply_footer = data.get("reply_footer")
    if reply_footer is not None:
        if not isinstance(reply_footer, dict):
            errors.append("reply_footer: must be a mapping")
        else:
            allowed_reply_footer = {
                "enabled",
                "emoji",
                "show_model",
                "show_session",
                "show_elapsed",
                "session_chars",
                "separator",
            }
            for key in reply_footer:
                if key not in allowed_reply_footer:
                    errors.append(f"reply_footer.{key}: unknown field")
            for key in ("enabled", "show_model", "show_session", "show_elapsed"):
                value = reply_footer.get(key)
                if value is not None and not isinstance(value, bool):
                    errors.append(f"reply_footer.{key}: must be boolean")
            emoji = reply_footer.get("emoji")
            if emoji is not None and (
                not isinstance(emoji, str) or not emoji or len(emoji) > 8
            ):
                errors.append("reply_footer.emoji: must be a non-empty string <= 8 chars")
            separator = reply_footer.get("separator")
            if separator is not None and (
                not isinstance(separator, str) or not separator or len(separator) > 8
            ):
                errors.append("reply_footer.separator: must be a non-empty string <= 8 chars")
            session_chars = reply_footer.get("session_chars")
            if session_chars is not None and (
                not _is_int_like(session_chars)
                or int(session_chars) < 3
                or int(session_chars) > 64
            ):
                errors.append("reply_footer.session_chars: must be an integer between 3 and 64")
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
    if data.get("triage_unsafe_fallback_timeout_seconds") is not None:
        _validate_positive_int(
            errors,
            "triage_unsafe_fallback_timeout_seconds",
            data["triage_unsafe_fallback_timeout_seconds"],
        )
    if isinstance(data.get("triage_routing"), dict):
        for key, value in data["triage_routing"].items():
            _validate_brain_spec(errors, f"triage_routing.{key}", value)
    if isinstance(triage_raw, dict) and isinstance(triage_raw.get("routing"), dict):
        for key, value in triage_raw["routing"].items():
            _validate_brain_spec(errors, f"triage.routing.{key}", value)

    if isinstance(data.get("triage_backup"), dict):
        for key, value in data["triage_backup"].items():
            _validate_brain_spec(errors, f"triage_backup.{key}", value)

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
                "approvals",
                "state",
                "body_limit",
                "notify_on_unknown",  # Legacy no-op; accepted so old configs still load.
                "telegram_chat_id",
                # company-inbox channel.
                "inbox_status_filter",
                "max_new_per_tick",
                "emit_task_closed",
                # whatsapp channel: nested per-account dicts validated lazily by the channel itself.
                "accounts",
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
                if raw_value.get("emit_task_closed") is not None and not isinstance(
                    raw_value["emit_task_closed"], bool
                ):
                    errors.append(f"channels.{raw_key}.emit_task_closed: must be boolean")
                if raw_value.get("brain") is not None:
                    _validate_brain_spec(errors, f"channels.{raw_key}.brain", raw_value["brain"])
                    if (
                        isinstance(raw_value["brain"], str)
                        and ":" in raw_value["brain"]
                        and raw_value.get("model") is not None
                    ):
                        errors.append(
                            f"channels.{raw_key}.brain: includes a model; also "
                            "setting model is ambiguous (set only one)"
                        )
                for key in ("timeout_seconds", "poll_interval_seconds", "max_new_per_tick"):
                    if raw_value.get(key) is not None:
                        _validate_positive_int(errors, f"channels.{raw_key}.{key}", raw_value[key])
                if raw_value.get("inbox_status_filter") is not None and not isinstance(
                    raw_value["inbox_status_filter"], (str, list, tuple)
                ):
                    errors.append(
                        f"channels.{raw_key}.inbox_status_filter: must be a string or list"
                    )
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
                    if key not in {"bin", "sandbox", "yolo", "timeout_seconds", "extra_args", "no_tools", "thinking", "vision_model"}:
                        errors.append(f"brains.{name}.{key}: unknown field")
                if body.get("sandbox") is not None:
                    sandbox = str(body["sandbox"])
                    if str(name) == "codex" and sandbox not in CODEX_SANDBOX_VALUES:
                        errors.append(
                            "brains.codex.sandbox: must be one of "
                            "read-only, workspace-write, yolo, danger, danger-full-access"
                        )
                if body.get("yolo") is not None and not isinstance(body["yolo"], bool):
                    errors.append(f"brains.{name}.yolo: must be boolean")
                if body.get("no_tools") is not None and not isinstance(body["no_tools"], bool):
                    errors.append(f"brains.{name}.no_tools: must be boolean")
                if body.get("thinking") is not None:
                    thinking_val = str(body["thinking"]).strip().lower()
                    if thinking_val not in ("off", "minimal", "low", "medium", "high", "xhigh"):
                        errors.append(
                            f"brains.{name}.thinking: must be one of "
                            "off, minimal, low, medium, high, xhigh"
                        )
                if (
                    str(name) == "codex"
                    and body.get("yolo") is True
                    and body.get("sandbox") is not None
                    and str(body["sandbox"]) not in CODEX_YOLO_SANDBOX_VALUES
                ):
                    errors.append(
                        "brains.codex: yolo=true conflicts with non-yolo sandbox"
                    )
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
                if key not in {
                    "max_queue_depth",
                    "log_max_bytes",
                    "log_backups",
                    "backoff_seconds",
                    "coalesce_same_conversation",
                }:
                    errors.append(f"reliability.{key}: unknown field")
            for key in ("max_queue_depth", "log_max_bytes"):
                if reliability.get(key) is not None:
                    _validate_positive_int(errors, f"reliability.{key}", reliability[key])
            if reliability.get("log_backups") is not None:
                _validate_nonnegative_int(errors, "reliability.log_backups", reliability["log_backups"])
            if reliability.get("coalesce_same_conversation") is not None and not isinstance(
                reliability["coalesce_same_conversation"], bool
            ):
                errors.append("reliability.coalesce_same_conversation: must be boolean")
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

    principal_raw = data.get("principal")
    if principal_raw is not None:
        if not isinstance(principal_raw, dict):
            errors.append("principal: must be a mapping")
        else:
            allowed_principal = {
                "telegram_chat_id",
                "telegram_user_id",
                "email",
                "email_domain",
            }
            for key in principal_raw:
                if key not in allowed_principal:
                    errors.append(f"principal.{key}: unknown field")
            for key in ("telegram_chat_id", "telegram_user_id"):
                value = principal_raw.get(key)
                if value is not None and not isinstance(value, (str, int)):
                    errors.append(f"principal.{key}: must be a string or integer")
            for key in ("email", "email_domain"):
                value = principal_raw.get(key)
                if value is not None and not isinstance(value, str):
                    errors.append(f"principal.{key}: must be a string")

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

    accountabilities_raw = data.get("accountabilities")
    if accountabilities_raw is not None:
        if not isinstance(accountabilities_raw, dict):
            errors.append("accountabilities: must be a mapping")
        else:
            allowed_accountabilities = {
                "enabled",
                "authority_channel",
                "enactment_token",
                "authority_email_sender",
            }
            for key in accountabilities_raw:
                if key not in allowed_accountabilities:
                    errors.append(f"accountabilities.{key}: unknown field")
            enabled = accountabilities_raw.get("enabled", False)
            if enabled is not None and not isinstance(enabled, bool):
                errors.append("accountabilities.enabled: must be boolean")
            if enabled is True:
                channel = accountabilities_raw.get("authority_channel", "telegram-primary")
                if not isinstance(channel, str) or channel not in ACCOUNTABILITIES_AUTHORITY_CHANNELS:
                    supported = ", ".join(ACCOUNTABILITIES_AUTHORITY_CHANNELS)
                    errors.append(
                        f"accountabilities.authority_channel: must be one of {supported}"
                    )
                token = accountabilities_raw.get("enactment_token", "OK enact")
                if not isinstance(token, str) or not token.strip():
                    errors.append("accountabilities.enactment_token: must be a non-empty string")
                sender = accountabilities_raw.get("authority_email_sender", "")
                if sender is not None and not isinstance(sender, str):
                    errors.append("accountabilities.authority_email_sender: must be a string")
                if channel == "email" and (not isinstance(sender, str) or not sender.strip()):
                    errors.append(
                        "accountabilities.authority_email_sender: required when "
                        "authority_channel is 'email'"
                    )

    entities_raw = data.get("entities")
    if entities_raw is not None:
        if not isinstance(entities_raw, dict):
            errors.append("entities: must be a mapping")
        else:
            allowed_entities = {"enabled", "migrate_legacy_people"}
            for key in entities_raw:
                if key not in allowed_entities:
                    errors.append(f"entities.{key}: unknown field")
            for key in ("enabled", "migrate_legacy_people"):
                value = entities_raw.get(key)
                if value is not None and not isinstance(value, bool):
                    errors.append(f"entities.{key}: must be boolean")

    inter_agent_raw = data.get("inter_agent_protocol")
    if inter_agent_raw is not None:
        if not isinstance(inter_agent_raw, dict):
            errors.append("inter_agent_protocol: must be a mapping")
        else:
            allowed_inter_agent = {
                "enabled",
                "authority_map_path",
                "require_self_declaration",
            }
            for key in inter_agent_raw:
                if key not in allowed_inter_agent:
                    errors.append(f"inter_agent_protocol.{key}: unknown field")
            for key in ("enabled", "require_self_declaration"):
                value = inter_agent_raw.get(key)
                if value is not None and not isinstance(value, bool):
                    errors.append(f"inter_agent_protocol.{key}: must be boolean")
            path_value = inter_agent_raw.get("authority_map_path")
            if path_value is not None and (
                not isinstance(path_value, str) or not path_value.strip()
            ):
                errors.append(
                    "inter_agent_protocol.authority_map_path: must be a non-empty string"
                )

    adaptive_raw = data.get("adaptive_discovery")
    if adaptive_raw is not None:
        if not isinstance(adaptive_raw, dict):
            errors.append("adaptive_discovery: must be a mapping")
        else:
            allowed_adaptive = {
                "enabled",
                "default_unknown_posture",
                "high_stakes_escalation_channel",
            }
            for key in adaptive_raw:
                if key not in allowed_adaptive:
                    errors.append(f"adaptive_discovery.{key}: unknown field")
            enabled_value = adaptive_raw.get("enabled")
            if enabled_value is not None and not isinstance(enabled_value, bool):
                errors.append("adaptive_discovery.enabled: must be boolean")
            posture = adaptive_raw.get("default_unknown_posture")
            if posture is not None:
                if not isinstance(posture, str) or posture not in ADAPTIVE_DISCOVERY_UNKNOWN_POSTURES:
                    supported = ", ".join(ADAPTIVE_DISCOVERY_UNKNOWN_POSTURES)
                    errors.append(
                        f"adaptive_discovery.default_unknown_posture: must be one of {supported}"
                    )
            channel = adaptive_raw.get("high_stakes_escalation_channel")
            if channel is not None:
                if not isinstance(channel, str) or not channel.strip():
                    errors.append(
                        "adaptive_discovery.high_stakes_escalation_channel: must be a non-empty string"
                    )
                elif channel != ADAPTIVE_DISCOVERY_AUTHORITY_ALIAS and (
                    _normalize_channel_key(channel) not in SUPPORTED_CHANNELS
                ):
                    supported = ", ".join(
                        (ADAPTIVE_DISCOVERY_AUTHORITY_ALIAS, *SUPPORTED_CHANNELS)
                    )
                    errors.append(
                        f"adaptive_discovery.high_stakes_escalation_channel: must be one of {supported}"
                    )

    parallel_raw = data.get("parallel")
    if parallel_raw is not None:
        if not isinstance(parallel_raw, dict):
            errors.append("parallel: must be a mapping")
        else:
            allowed_parallel = {
                "max_concurrent",
                "transcript_context_lines",
                "classifier",
            }
            for key in parallel_raw:
                if key not in allowed_parallel:
                    errors.append(f"parallel.{key}: unknown field")
            max_concurrent = parallel_raw.get("max_concurrent")
            if max_concurrent is not None:
                if not _is_int_like(max_concurrent) or int(max_concurrent) < 1 or int(max_concurrent) > 20:
                    errors.append("parallel.max_concurrent: must be an integer between 1 and 20")
            ctx_lines = parallel_raw.get("transcript_context_lines")
            if ctx_lines is not None:
                _validate_nonnegative_int(
                    errors, "parallel.transcript_context_lines", ctx_lines
                )
            classifier_raw = parallel_raw.get("classifier")
            if classifier_raw is not None:
                if not isinstance(classifier_raw, dict):
                    errors.append("parallel.classifier: must be a mapping")
                else:
                    allowed_classifier = {
                        "backend",
                        "model",
                        "timeout_seconds",
                        "cache_ttl_seconds",
                    }
                    for key in classifier_raw:
                        if key not in allowed_classifier:
                            errors.append(
                                f"parallel.classifier.{key}: unknown field"
                            )
                    backend_val = classifier_raw.get("backend")
                    if backend_val is not None and backend_val != "openrouter":
                        errors.append(
                            "parallel.classifier.backend: only 'openrouter' is supported"
                        )
                    model_val = classifier_raw.get("model")
                    if model_val is not None and (
                        not isinstance(model_val, str) or not model_val.strip()
                    ):
                        errors.append("parallel.classifier.model: must be a non-empty string")
                    for key in ("timeout_seconds", "cache_ttl_seconds"):
                        if classifier_raw.get(key) is not None:
                            _validate_positive_int(
                                errors, f"parallel.classifier.{key}", classifier_raw[key]
                            )

    actions_raw = data.get("actions")
    if actions_raw is not None:
        if not isinstance(actions_raw, dict):
            errors.append("actions: must be a mapping")
        else:
            allowed_actions = {
                "enabled",
                "stop_grace_seconds",
                "max_background_per_chat",
                "suppress_background_tool_messages",
            }
            for key in actions_raw:
                if key not in allowed_actions:
                    errors.append(f"actions.{key}: unknown field")
            if actions_raw.get("enabled") is not None and not isinstance(
                actions_raw["enabled"], bool
            ):
                errors.append("actions.enabled: must be boolean")
            if actions_raw.get("stop_grace_seconds") is not None:
                _validate_positive_int(
                    errors,
                    "actions.stop_grace_seconds",
                    actions_raw["stop_grace_seconds"],
                )
            if actions_raw.get("max_background_per_chat") is not None:
                _validate_positive_int(
                    errors,
                    "actions.max_background_per_chat",
                    actions_raw["max_background_per_chat"],
                )
            if actions_raw.get("suppress_background_tool_messages") is not None and not isinstance(
                actions_raw["suppress_background_tool_messages"], bool
            ):
                errors.append("actions.suppress_background_tool_messages: must be boolean")

    _validate_session_lifecycle(errors, data.get("session_lifecycle"))

    compaction_notify_raw = data.get("compaction_notify")
    if compaction_notify_raw is not None:
        if not isinstance(compaction_notify_raw, dict):
            errors.append("compaction_notify: must be a mapping")
        else:
            for key in compaction_notify_raw:
                if key != "enabled":
                    errors.append(f"compaction_notify.{key}: unknown field")
            if compaction_notify_raw.get("enabled") is not None and not isinstance(
                compaction_notify_raw["enabled"], bool
            ):
                errors.append("compaction_notify.enabled: must be boolean")

    if errors:
        raise ConfigError("; ".join(errors))


def _validate_session_lifecycle(errors: list[str], raw: Any) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        errors.append("session_lifecycle: must be a mapping")
        return
    allowed = {
        "enabled",
        "thresholds",
        "reserves",
        "native_compaction",
        "model_profiles",
        "idle",
        "checkpoint",
        "fallback_limits",
        "tool_results",
    }
    for key in raw:
        if key not in allowed:
            errors.append(f"session_lifecycle.{key}: unknown field")
    for key in ("idle", "checkpoint", "fallback_limits", "tool_results"):
        if raw.get(key) is not None:
            errors.append(f"session_lifecycle.{key}: field {key} not yet supported")
    if raw.get("enabled") is not None and not isinstance(raw["enabled"], bool):
        errors.append("session_lifecycle.enabled: must be boolean")

    th = raw.get("thresholds")
    if th is not None:
        if not isinstance(th, dict):
            errors.append("session_lifecycle.thresholds: must be a mapping")
        else:
            try:
                candidate = Thresholds(
                    observe_ratio=float(th.get("observe_ratio", Thresholds().observe_ratio)),
                    idle_maintenance_ratio=float(
                        th.get("idle_maintenance_ratio", Thresholds().idle_maintenance_ratio)
                    ),
                    rotate_ratio=float(th.get("rotate_ratio", Thresholds().rotate_ratio)),
                    emergency_ratio=float(
                        th.get("emergency_ratio", Thresholds().emergency_ratio)
                    ),
                )
            except (TypeError, ValueError):
                errors.append("session_lifecycle.thresholds: ratios must be numbers")
            else:
                for msg in candidate.validate():
                    errors.append(f"session_lifecycle.thresholds.{msg}")

    rv = raw.get("reserves")
    if rv is not None:
        if not isinstance(rv, dict):
            errors.append("session_lifecycle.reserves: must be a mapping")
        else:
            try:
                candidate_rv = Reserves(
                    output_tokens=int(rv.get("output_tokens", Reserves().output_tokens)),
                    turn_input_tokens=int(
                        rv.get("turn_input_tokens", Reserves().turn_input_tokens)
                    ),
                )
            except (TypeError, ValueError):
                errors.append("session_lifecycle.reserves: must be integers")
            else:
                for msg in candidate_rv.validate():
                    errors.append(f"session_lifecycle.reserves.{msg}")

    profiles = raw.get("model_profiles")
    if profiles is not None and not isinstance(profiles, dict):
        errors.append("session_lifecycle.model_profiles: must be a mapping")
    elif isinstance(profiles, dict):
        for pkey, spec in profiles.items():
            if not isinstance(spec, dict):
                errors.append(f"session_lifecycle.model_profiles.{pkey}: must be a mapping")
                continue
            cap = spec.get("input_capacity_tokens")
            if cap is not None:
                _validate_positive_int(
                    errors, f"session_lifecycle.model_profiles.{pkey}.input_capacity_tokens", cap
                )
            for flag in ("extended_context", "enabled", "allow_capacity_upgrade", "requires_credits"):
                if spec.get(flag) is not None and not isinstance(spec[flag], bool):
                    errors.append(
                        f"session_lifecycle.model_profiles.{pkey}.{flag}: must be boolean"
                    )


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
    parsed = _brain_spec.parse(str(brain_value) if brain_value is not None else None)
    brain = parsed.brain if parsed.brain in SUPPORTED_BRAINS else None
    explicit_model = str(raw["model"]) if raw.get("model") is not None else None
    channel_model = (
        parsed.model
        if parsed.model is not None
        else (explicit_model if explicit_model is not None else defaults.model)
    )

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
        model=channel_model,
        timeout_seconds=int(raw.get("timeout_seconds") or defaults.timeout_seconds),
        watch_dir=_opt_str("watch_dir", defaults.watch_dir),
        poll_interval_seconds=_opt_int("poll_interval_seconds", defaults.poll_interval_seconds),
        paired_with=_opt_str("paired_with", defaults.paired_with),
        asr_provider=_opt_str("asr_provider", defaults.asr_provider),
        tts_provider=_opt_str("tts_provider", defaults.tts_provider),
        inbox_status_filter=_tuple_str(raw.get("inbox_status_filter", defaults.inbox_status_filter)),
        max_new_per_tick=_opt_int("max_new_per_tick", defaults.max_new_per_tick),
        emit_task_closed=bool(raw.get("emit_task_closed", defaults.emit_task_closed)),
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
    routing: dict[str, str] = dict(DEFAULT_TRIAGE_ROUTING)
    if isinstance(routing_raw, dict):
        for key, value in routing_raw.items():
            if isinstance(value, str):
                routing[str(key)] = value

    def _opt(key: str, default: Any) -> Any:
        return data.get(key, raw.get(key, default))

    def _opt_pair(top: str, nested: str, default: Any) -> Any:
        return data.get(top, raw.get(nested, raw.get(top, default)))

    unsafe_fallback = _opt("triage_unsafe_fallback_brain", "")
    if unsafe_fallback is None:
        unsafe_fallback = ""

    backup_raw = data.get("triage_backup") or {}
    backup: dict[str, str] = {}
    if isinstance(backup_raw, dict):
        for _bk, _bv in backup_raw.items():
            if isinstance(_bv, str) and _bv:
                backup[str(_bk)] = _bv

    return TriageConfig(
        backend=str(backend or "none"),
        confidence_threshold=float(_opt("triage_confidence_threshold", 0.7)),
        fallback_brain=str(_opt("default_fallback_brain", "claude:sonnet-4-6")),
        unsafe_fallback_brain=str(unsafe_fallback),
        unsafe_fallback_timeout_seconds=int(_opt("triage_unsafe_fallback_timeout_seconds", 60)),
        cache_ttl_seconds=int(_opt("triage_cache_ttl_seconds", 30)),
        sticky_idle_seconds=int(_opt("sticky_brain_idle_timeout_seconds", 0)),
        routing=routing,
        ollama_model=str(_opt("ollama_model", "phi3:mini")),
        ollama_host=str(_opt("ollama_host", "http://localhost:11434")),
        ollama_timeout_seconds=int(_opt("ollama_timeout_seconds", 5)),
        openrouter_model=str(_opt("openrouter_model", "meta-llama/llama-3.1-8b-instruct")),
        openrouter_api_key_env=str(_opt("openrouter_api_key_env", "OPENROUTER_API_KEY")),
        openrouter_timeout_seconds=int(_opt("openrouter_timeout_seconds", 5)),
        protocol=str(_opt_pair("triage_protocol", "protocol", "openai_compat")),
        base_url=str(_opt_pair("triage_base_url", "base_url", "")),
        api_key_env=str(_opt_pair("triage_api_key_env", "api_key_env", "")),
        model=str(_opt_pair("triage_model", "model", "")),
        timeout_seconds=int(_opt_pair("triage_timeout_seconds", "timeout_seconds", 5)),
        max_tokens=(
            int(_opt_pair("triage_max_tokens", "max_tokens", 0))
            if _opt_pair("triage_max_tokens", "max_tokens", None) is not None
            else None
        ),
        claude_triage_screen=str(_opt("claude_triage_screen", "jc-triage")),
        claude_triage_model=str(_opt("claude_triage_model", "claude-haiku-4-5")),
        claude_triage_port=int(_opt("claude_triage_port", 9876)),
        backup=backup,
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
            no_tools=bool(body["no_tools"]) if body.get("no_tools") is not None else None,
            thinking=str(body["thinking"]) if body.get("thinking") is not None else None,
            vision_model=str(body["vision_model"]) if body.get("vision_model") is not None else None,
        )
    return out


def _load_reply_footer(data: dict[str, Any]) -> ReplyFooterConfig:
    raw = data.get("reply_footer") if isinstance(data.get("reply_footer"), dict) else {}
    return ReplyFooterConfig(
        enabled=bool(raw.get("enabled", DEFAULT_CONFIG.reply_footer.enabled)),
        emoji=str(raw.get("emoji", DEFAULT_CONFIG.reply_footer.emoji)),
        show_model=bool(raw.get("show_model", DEFAULT_CONFIG.reply_footer.show_model)),
        show_session=bool(raw.get("show_session", DEFAULT_CONFIG.reply_footer.show_session)),
        show_elapsed=bool(raw.get("show_elapsed", DEFAULT_CONFIG.reply_footer.show_elapsed)),
        session_chars=int(raw.get("session_chars", DEFAULT_CONFIG.reply_footer.session_chars)),
        separator=str(raw.get("separator", DEFAULT_CONFIG.reply_footer.separator)),
    )


def _load_principal(data: dict[str, Any]) -> PrincipalConfig:
    raw = data.get("principal") if isinstance(data.get("principal"), dict) else {}

    def _str_or_none(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    return PrincipalConfig(
        telegram_chat_id=_str_or_none(raw.get("telegram_chat_id")),
        telegram_user_id=_str_or_none(raw.get("telegram_user_id")),
        email=_str_or_none(raw.get("email")),
        email_domain=_str_or_none(raw.get("email_domain")),
    )


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


def _load_accountabilities(data: dict[str, Any]) -> AccountabilitiesConfig:
    raw = data.get("accountabilities") if isinstance(data.get("accountabilities"), dict) else {}
    defaults = AccountabilitiesConfig()
    return AccountabilitiesConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        authority_channel=str(raw.get("authority_channel") or defaults.authority_channel),
        enactment_token=str(raw.get("enactment_token") or defaults.enactment_token),
        authority_email_sender=str(
            raw.get("authority_email_sender")
            if raw.get("authority_email_sender") is not None
            else defaults.authority_email_sender
        ),
    )


def _load_entities(data: dict[str, Any]) -> EntitiesConfig:
    raw = data.get("entities") if isinstance(data.get("entities"), dict) else {}
    defaults = EntitiesConfig()
    return EntitiesConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        migrate_legacy_people=bool(
            raw.get("migrate_legacy_people", defaults.migrate_legacy_people)
        ),
    )


def _load_inter_agent_protocol(data: dict[str, Any]) -> InterAgentProtocolConfig:
    raw = (
        data.get("inter_agent_protocol")
        if isinstance(data.get("inter_agent_protocol"), dict)
        else {}
    )
    defaults = InterAgentProtocolConfig()
    return InterAgentProtocolConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        authority_map_path=str(
            raw.get("authority_map_path") or defaults.authority_map_path
        ),
        require_self_declaration=bool(
            raw.get("require_self_declaration", defaults.require_self_declaration)
        ),
    )


def _load_adaptive_discovery(data: dict[str, Any]) -> AdaptiveDiscoveryConfig:
    raw = (
        data.get("adaptive_discovery")
        if isinstance(data.get("adaptive_discovery"), dict)
        else {}
    )
    defaults = AdaptiveDiscoveryConfig()
    raw_channel = str(
        raw.get("high_stakes_escalation_channel")
        or defaults.high_stakes_escalation_channel
    )
    # Normalize channel slugs (e.g. `jc_events` → `jc-events`) so consumers can
    # compare against SUPPORTED_CHANNELS directly. The `authority` alias is
    # preserved verbatim since it is not a channel slug.
    if raw_channel != ADAPTIVE_DISCOVERY_AUTHORITY_ALIAS:
        raw_channel = _normalize_channel_key(raw_channel)
    return AdaptiveDiscoveryConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        default_unknown_posture=str(
            raw.get("default_unknown_posture") or defaults.default_unknown_posture
        ),
        high_stakes_escalation_channel=raw_channel,
    )


def _load_parallel(data: dict[str, Any]) -> ParallelConfig:
    raw = data.get("parallel") if isinstance(data.get("parallel"), dict) else {}
    defaults = ParallelConfig()
    classifier_raw = raw.get("classifier") if isinstance(raw.get("classifier"), dict) else {}
    classifier = ParallelClassifierConfig(
        backend=str(classifier_raw.get("backend") or defaults.classifier.backend),
        model=str(classifier_raw.get("model") or defaults.classifier.model),
        timeout_seconds=int(
            classifier_raw.get("timeout_seconds")
            if classifier_raw.get("timeout_seconds") is not None
            else defaults.classifier.timeout_seconds
        ),
        cache_ttl_seconds=int(
            classifier_raw.get("cache_ttl_seconds")
            if classifier_raw.get("cache_ttl_seconds") is not None
            else defaults.classifier.cache_ttl_seconds
        ),
    )
    return ParallelConfig(
        max_concurrent=int(
            raw.get("max_concurrent")
            if raw.get("max_concurrent") is not None
            else defaults.max_concurrent
        ),
        transcript_context_lines=int(
            raw.get("transcript_context_lines")
            if raw.get("transcript_context_lines") is not None
            else defaults.transcript_context_lines
        ),
        classifier=classifier,
    )


def _load_actions(data: dict[str, Any]) -> ActionsConfig:
    raw = data.get("actions") if isinstance(data.get("actions"), dict) else {}
    defaults = ActionsConfig()
    return ActionsConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        stop_grace_seconds=int(
            raw.get("stop_grace_seconds")
            if raw.get("stop_grace_seconds") is not None
            else defaults.stop_grace_seconds
        ),
        max_background_per_chat=int(
            raw.get("max_background_per_chat")
            if raw.get("max_background_per_chat") is not None
            else defaults.max_background_per_chat
        ),
        suppress_background_tool_messages=bool(
            raw.get("suppress_background_tool_messages", defaults.suppress_background_tool_messages)
        ),
    )


def _load_session_lifecycle(data: dict[str, Any]) -> SessionLifecycleConfig:
    raw = data.get("session_lifecycle") if isinstance(data.get("session_lifecycle"), dict) else {}
    defaults = SessionLifecycleConfig()
    th_raw = raw.get("thresholds") if isinstance(raw.get("thresholds"), dict) else {}
    thresholds = Thresholds(
        observe_ratio=float(th_raw.get("observe_ratio", defaults.thresholds.observe_ratio)),
        idle_maintenance_ratio=float(
            th_raw.get("idle_maintenance_ratio", defaults.thresholds.idle_maintenance_ratio)
        ),
        rotate_ratio=float(th_raw.get("rotate_ratio", defaults.thresholds.rotate_ratio)),
        emergency_ratio=float(th_raw.get("emergency_ratio", defaults.thresholds.emergency_ratio)),
    )
    rv_raw = raw.get("reserves") if isinstance(raw.get("reserves"), dict) else {}
    reserves = Reserves(
        output_tokens=int(rv_raw.get("output_tokens", defaults.reserves.output_tokens)),
        turn_input_tokens=int(rv_raw.get("turn_input_tokens", defaults.reserves.turn_input_tokens)),
    )
    nc_raw = raw.get("native_compaction") if isinstance(raw.get("native_compaction"), dict) else {}
    native_compaction = NativeCompactionConfig(
        enabled=bool(nc_raw.get("enabled", defaults.native_compaction.enabled)),
        fallback_to_rotation=bool(
            nc_raw.get("fallback_to_rotation", defaults.native_compaction.fallback_to_rotation)
        ),
    )
    profiles_raw = raw.get("model_profiles") if isinstance(raw.get("model_profiles"), dict) else {}
    model_profiles = tuple(
        (str(key), value) for key, value in profiles_raw.items() if isinstance(value, dict)
    )
    return SessionLifecycleConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        thresholds=thresholds,
        reserves=reserves,
        native_compaction=native_compaction,
        model_profiles=model_profiles,
    )


def _load_compaction_notify(data: dict[str, Any]) -> CompactionNotifyConfig:
    raw = data.get("compaction_notify") if isinstance(data.get("compaction_notify"), dict) else {}
    defaults = CompactionNotifyConfig()
    return CompactionNotifyConfig(enabled=bool(raw.get("enabled", defaults.enabled)))


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
        coalesce_same_conversation=bool(raw.get("coalesce_same_conversation", False)),
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

    raw_default_brain = data.get("default_brain")
    parsed_default = _brain_spec.parse(
        str(raw_default_brain) if raw_default_brain is not None else None
    )
    if parsed_default.brain in SUPPORTED_BRAINS:
        default_brain = parsed_default.brain
        default_model_from_spec = parsed_default.model
    else:
        # _validate_raw_config catches unknown brains before we reach here, so
        # this branch only runs when default_brain is unset.
        default_brain = DEFAULT_CONFIG.default_brain
        default_model_from_spec = None

    explicit_default_model = (
        str(data["default_model"])
        if data.get("default_model") is not None and not isinstance(data.get("default_model"), dict)
        else None
    )
    default_model = (
        default_model_from_spec
        if default_model_from_spec is not None
        else explicit_default_model
    )

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

    timezone_value = data.get("timezone")
    if isinstance(timezone_value, str) and timezone_value.strip():
        timezone_str = timezone_value.strip()
    else:
        timezone_str = DEFAULT_CONFIG.timezone

    return GatewayConfig(
        default_brain=default_brain,
        default_model=default_model,
        pin_to_default_brain=bool(data.get("pin_to_default_brain", False)),
        poll_interval_seconds=float(gateway.get("poll_interval_seconds") or DEFAULT_CONFIG.poll_interval_seconds),
        lease_seconds=int(gateway.get("lease_seconds") or DEFAULT_CONFIG.lease_seconds),
        max_retries=int(gateway.get("max_retries") or DEFAULT_CONFIG.max_retries),
        adapter_timeout_seconds=int(
            gateway.get("adapter_timeout_seconds") or DEFAULT_CONFIG.adapter_timeout_seconds
        ),
        timezone=timezone_str,
        channels=channels,
        triage=_load_triage(data),
        reply_footer=_load_reply_footer(data),
        brains=_load_brains(data),
        reliability=_load_reliability(data),
        codex_auth=_load_codex_auth(data),
        principal=_load_principal(data),
        accountabilities=_load_accountabilities(data),
        entities=_load_entities(data),
        inter_agent_protocol=_load_inter_agent_protocol(data),
        adaptive_discovery=_load_adaptive_discovery(data),
        parallel=_load_parallel(data),
        actions=_load_actions(data),
        session_lifecycle=_load_session_lifecycle(data),
        compaction_notify=_load_compaction_notify(data),
    )


def render_default_config(
    *,
    default_brain: str = "claude",
    telegram_enabled: bool = False,
    telegram_chat_id: str = "",
    slack_enabled: bool = False,
    discord_enabled: bool = False,
    triage_backend: str = "none",
    timezone: str = "UTC",
) -> str:
    telegram_chats = f"[{telegram_chat_id}]" if telegram_chat_id else "[]"
    return f"""# JuliusCaesar gateway runtime config. Secrets live in .env.
default_brain: {default_brain}
default_model: null
pin_to_default_brain: false
# IANA name (e.g. Asia/Dubai). Used for time injection into brain prompts and heartbeat templates.
timezone: {timezone}
gateway:
  poll_interval_seconds: 1
  lease_seconds: 300
  max_retries: 3
  adapter_timeout_seconds: 300
triage: {triage_backend}
triage_confidence_threshold: 0.7
default_fallback_brain: claude:sonnet-4-6
triage_unsafe_fallback_brain: null
triage_unsafe_fallback_timeout_seconds: 60
sticky_brain_idle_timeout_seconds: 0
reply_footer:
  enabled: false
  emoji: "⚙️"
  show_model: true
  show_session: true
  show_elapsed: true
  session_chars: 8
# parallel: per-conversation slots for concurrent brain dispatch.
# Default max_concurrent=1 keeps strictly-serial (today's) behavior.
# Increase to allow N concurrent brain invocations per conversation —
# unrelated quick messages don't queue behind long-running ones.
# See docs/specs/parallel-slots.md.
parallel:
  max_concurrent: 1
  transcript_context_lines: 20
  classifier:
    backend: openrouter
    model: deepseek/deepseek-chat
    timeout_seconds: 3
    cache_ttl_seconds: 30
triage_routing:
  smalltalk: claude:haiku-4-5
  quick: claude:sonnet-4-6
  analysis: claude:opus-4-7-1m
  code: claude:sonnet-4-6
  image: claude:sonnet-4-6
  voice: claude:sonnet-4-6
  system: claude:haiku-4-5
# session_lifecycle: context-aware session lifecycle (docs/specs/context-aware-session-lifecycle.md).
# Disabled by default — context-limit errors still stop generic retry amplification.
# Enable to measure context pressure and rotate provider sessions before they exhaust.
session_lifecycle:
  enabled: false
  thresholds:
    observe_ratio: 0.50
    idle_maintenance_ratio: 0.60
    rotate_ratio: 0.70
    emergency_ratio: 0.85
  reserves:
    output_tokens: 16000
    turn_input_tokens: 12000
  native_compaction:
    enabled: true
    fallback_to_rotation: true
# compaction_notify: Telegram message to the operator's main chat on every compaction.
compaction_notify:
  enabled: true
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
  # company-inbox: pull task-graph assignments from the-company and inject
  # them as `company.task_assigned` events. Opt-in; reuses COMPANY_* creds
  # from .env (same as the supervisor reporter). See docs/specs/company-inbox-channel.md.
  company-inbox:
    enabled: false
    poll_interval_seconds: 10
    max_new_per_tick: 5
    inbox_status_filter: [pending, accepted]
"""
