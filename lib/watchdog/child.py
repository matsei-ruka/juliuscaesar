"""ChildSpec / ChildState — config + runtime state records for the supervisor."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HealthSpec:
    pid_alive: bool = False
    cwd_match: str | None = None
    proc_match: str | None = None
    heartbeat_file: str | None = None
    heartbeat_max_age_seconds: int = 30


@dataclass(frozen=True)
class RestartSpec:
    backoff: tuple[int, ...] = (5, 10, 30, 60, 300)
    max_in_window: int = 5
    window_seconds: int = 600
    start_grace_seconds: int = 15


@dataclass(frozen=True)
class ChildSpec:
    name: str
    type: str  # "daemon" | "legacy-claude" | "http-daemon"
    enabled: bool = True
    start: str | None = None
    pidfile: str | None = None
    screen_name: str | None = None
    session_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    health: HealthSpec = field(default_factory=HealthSpec)
    restart: RestartSpec = field(default_factory=RestartSpec)


@dataclass
class ChildState:
    name: str
    last_attempt_at: float = 0.0
    last_started_at: float = 0.0
    consecutive_failures: int = 0
    attempts_in_window: list[float] = field(default_factory=list)
    alert_mode: bool = False
    last_failure: str = ""
    last_pid: int | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, name: str, raw: dict[str, Any]) -> "ChildState":
        return cls(
            name=name,
            last_attempt_at=float(raw.get("last_attempt_at") or 0.0),
            last_started_at=float(raw.get("last_started_at") or 0.0),
            consecutive_failures=int(raw.get("consecutive_failures") or 0),
            attempts_in_window=[float(t) for t in raw.get("attempts_in_window") or []],
            alert_mode=bool(raw.get("alert_mode") or False),
            last_failure=str(raw.get("last_failure") or ""),
            last_pid=int(raw["last_pid"]) if raw.get("last_pid") is not None else None,
        )


def state_dir(instance_dir: Path) -> Path:
    return instance_dir / "state" / "watchdog"


def state_path(instance_dir: Path) -> Path:
    return state_dir(instance_dir) / "state.json"


def lock_path(instance_dir: Path) -> Path:
    return state_dir(instance_dir) / "lock"


def log_dir(instance_dir: Path) -> Path:
    return state_dir(instance_dir) / "logs"


class StateStore:
    """Persists per-child state to a single JSON file."""

    def __init__(self, instance_dir: Path):
        self.path = state_path(instance_dir)
        self._cache: dict[str, ChildState] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for name, body in raw.items():
            if isinstance(body, dict):
                self._cache[str(name)] = ChildState.from_json(str(name), body)

    def get(self, name: str) -> ChildState:
        if name not in self._cache:
            self._cache[name] = ChildState(name=name)
        return self._cache[name]

    def all(self) -> dict[str, ChildState]:
        return dict(self._cache)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {name: state.to_json() for name, state in self._cache.items()}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def reset(self, name: str) -> None:
        self._cache[name] = ChildState(name=name)
