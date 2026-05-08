"""OpenCode brain wrapper."""

from __future__ import annotations

import json
import subprocess

from .base import Brain, parse_iso


def _parse_opencode_time(value: object) -> float | None:
    if isinstance(value, (int, float)):
        # Current OpenCode emits Unix epoch milliseconds in `created`/`updated`.
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

    def capture_session_id(self, started_at: str) -> str | None:
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
