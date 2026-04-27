"""Company client configuration: endpoint + creds from .env, knobs from gateway.yaml.

Two layers:

1. ``.env`` (instance-local secrets): ``COMPANY_ENDPOINT``, ``COMPANY_API_KEY``,
   ``COMPANY_ENROLLMENT_TOKEN``.
2. ``ops/gateway.yaml`` ``company:`` block (privacy + outbox knobs).

``is_enabled(instance_dir)`` is the single gate the gateway runtime checks
before instantiating the reporter.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gateway.config import (  # type: ignore
    _load_raw,
    clear_env_cache,
    config_path,
    env_value,
)


DEFAULT_OUTBOX_MAX_MB = 500
DEFAULT_OUTBOX_MAX_AGE_HOURS = 168  # 7 days
DEFAULT_CONVERSATION_MAX_CHARS = 4000
HEARTBEAT_INTERVAL_SECONDS = 30.0
BATCH_MAX_EVENTS = 500
HTTP_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class CompanyConfig:
    """Resolved company config — what the reporter actually uses."""

    endpoint: str
    api_key: str
    enrollment_token: str
    enabled: bool = True
    redact_conversations: bool = True
    exclude_channels: tuple[str, ...] = ()
    exclude_users: tuple[str, ...] = ()
    conversation_max_chars: int = DEFAULT_CONVERSATION_MAX_CHARS
    outbox_max_mb: int = DEFAULT_OUTBOX_MAX_MB
    outbox_max_age_hours: int = DEFAULT_OUTBOX_MAX_AGE_HOURS
    framework: str = "juliuscaesar"
    instance_dir: Path = field(default_factory=lambda: Path("."))

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key) or bool(self.enrollment_token)


def _yaml_company_block(instance_dir: Path) -> dict[str, Any]:
    raw = _load_raw(config_path(instance_dir))
    block = raw.get("company")
    return block if isinstance(block, dict) else {}


def _tuple_str(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v)
    return (str(value),)


def load(instance_dir: Path) -> CompanyConfig:
    """Resolve current company config. Pure function over .env + gateway.yaml."""
    block = _yaml_company_block(instance_dir)

    return CompanyConfig(
        endpoint=env_value(instance_dir, "COMPANY_ENDPOINT").rstrip("/"),
        api_key=env_value(instance_dir, "COMPANY_API_KEY"),
        enrollment_token=env_value(instance_dir, "COMPANY_ENROLLMENT_TOKEN"),
        enabled=bool(block.get("enabled", True)),
        redact_conversations=bool(block.get("redact_conversations", True)),
        exclude_channels=_tuple_str(block.get("exclude_channels")),
        exclude_users=_tuple_str(block.get("exclude_users")),
        conversation_max_chars=int(
            block.get("conversation_max_chars") or DEFAULT_CONVERSATION_MAX_CHARS
        ),
        outbox_max_mb=int(block.get("outbox_max_mb") or DEFAULT_OUTBOX_MAX_MB),
        outbox_max_age_hours=int(
            block.get("outbox_max_age_hours") or DEFAULT_OUTBOX_MAX_AGE_HOURS
        ),
        instance_dir=instance_dir,
    )


def is_enabled(instance_dir: Path) -> bool:
    """True iff company reporter should start for this instance.

    Requires: endpoint set, ``enabled: true`` (default) in gateway.yaml,
    and at least one of ``COMPANY_API_KEY`` / ``COMPANY_ENROLLMENT_TOKEN``.
    """
    cfg = load(instance_dir)
    return bool(cfg.endpoint) and cfg.enabled and cfg.has_credentials


def instance_id(instance_dir: Path) -> str:
    """Stable, unique identifier for this instance.

    sha256 of the absolute instance directory path. Survives restarts;
    same agent always reports the same ``instance_id``.
    """
    abs_path = str(Path(instance_dir).resolve())
    return hashlib.sha256(abs_path.encode("utf-8")).hexdigest()


def instance_name(instance_dir: Path) -> str:
    """First heading of memory/L1/IDENTITY.md, or instance dir basename.

    The first ``# Heading`` is the agent's display name (e.g. "Rachel Zane").
    Falls back to ``instance_dir.name`` if the file is absent or empty.
    """
    identity = Path(instance_dir) / "memory" / "L1" / "IDENTITY.md"
    try:
        for line in identity.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
    except (FileNotFoundError, OSError):
        pass
    return Path(instance_dir).resolve().name


def framework_version() -> str:
    """Read JC version from the framework's pyproject.toml."""
    framework_root = Path(__file__).resolve().parents[2]
    pyproject = framework_root / "pyproject.toml"
    try:
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("version") and "=" in line:
                value = line.split("=", 1)[1].strip()
                return value.strip('"').strip("'")
    except (FileNotFoundError, OSError):
        pass
    return "0.0.0"


def write_env_keys(instance_dir: Path, *, set_keys: dict[str, str], unset_keys: tuple[str, ...] = ()) -> None:
    """Atomically update ``<instance>/.env`` with the given key changes.

    Used post-registration to persist the API key and remove the bootstrap
    token. Preserves all unrelated lines verbatim.
    """
    env_path = Path(instance_dir) / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(raw)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in unset_keys:
            continue
        if key in set_keys:
            out.append(f"{key}={_quote_env(set_keys[key])}")
            seen.add(key)
            continue
        out.append(raw)

    for key, value in set_keys.items():
        if key not in seen:
            out.append(f"{key}={_quote_env(value)}")

    tmp = env_path.with_suffix(env_path.suffix + ".company.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(env_path)
    try:
        env_path.chmod(0o600)
    except OSError:
        pass

    # Refresh gateway-side env cache so the next load() sees the new value.
    clear_env_cache()


def _quote_env(value: str) -> str:
    if value == "" or any(ch.isspace() for ch in value) or "#" in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value
