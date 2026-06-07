"""OpenCode brain wrapper (sst/opencode).

opencode auto-compacts internally at its `usable` threshold (model context
− 32k reserved output − 20k buffer). The framework does not drive rotation
for this brain; usage telemetry is recorded for cross-brain visibility only
(see PROVIDER_MANAGED_COMPACTION_BRAINS in lifecycle/routing.py).

Session id and turn token usage are surfaced via a sidecar JSON written by
the adapter (`$JC_USAGE_SIDECAR_PATH`), because opencode 1.16 does not emit
text/usage events on stdout when `--format json` is used — only the first
`step_start` event. The reply text and tokens both live in the SQLite store
at `~/.local/share/opencode/opencode.db` and the adapter extracts them
after the run completes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..config import env_value
from ..queue import Event
from .base import Brain, parse_iso


OPENCODE_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
)


def _parse_opencode_time(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / 1000
    if isinstance(value, str):
        return parse_iso(value)
    return None


def _opencode_session_time(session: dict) -> float | None:
    values = [
        _parse_opencode_time(session.get(key))
        for key in ("created_at", "started_at", "start", "created", "updated")
    ]
    values = [value for value in values if value is not None]
    return max(values) if values else None


class OpencodeBrain(Brain):
    name = "opencode"
    needs_l1_preamble = True
    goal_delivery = "system_prompt"

    def extra_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in OPENCODE_ENV_KEYS:
            value = env_value(self.instance_dir, key)
            if value:
                env[key] = value
        no_tools = env_value(self.instance_dir, "JC_OPENCODE_NO_TOOLS")
        if no_tools:
            env["JC_OPENCODE_NO_TOOLS"] = no_tools
        if self.override.no_tools:
            env["JC_OPENCODE_NO_TOOLS"] = "1"
        return env

    def extra_args_for_event(self, event: Event) -> tuple[str, ...]:
        meta = self._meta(event)
        paths: list[str] = []
        single = meta.get("image_path")
        if isinstance(single, str) and single.strip():
            paths.append(single.strip())
        multi = meta.get("image_paths")
        if isinstance(multi, list):
            paths.extend(str(item).strip() for item in multi if str(item).strip())
        args: list[str] = []
        for path in paths:
            args.extend(["--file", path])
        return tuple(args)

    def pre_invoke_snapshot(self) -> None:
        return None

    def capture_session_id(self, started_at: str) -> str | None:
        """Fallback session capture via `opencode session list --format json`.

        Primary path: adapter writes `session_id` into the usage sidecar; the
        runtime in `Brain.invoke` prefers that. This method runs only when the
        sidecar is missing or did not include a session id (e.g. the adapter
        failed mid-flight after spawning opencode).
        """
        t0 = parse_iso(started_at)
        if t0 is None:
            return None
        try:
            proc = subprocess.run(
                ["opencode", "session", "list", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.instance_dir),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        best = None
        best_delta = None
        for session in sessions:
            if not isinstance(session, dict):
                continue
            directory = session.get("directory")
            if isinstance(directory, str) and directory != str(self.instance_dir):
                continue
            st = _opencode_session_time(session)
            if st is None or st < t0:
                continue
            delta = st - t0
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best = session
        if not best:
            return None
        sid = best.get("id") or best.get("session_id")
        return str(sid) if sid is not None else None
