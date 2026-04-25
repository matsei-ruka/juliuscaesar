"""OpenCode brain wrapper."""

from __future__ import annotations

import json
import subprocess

from .base import Brain, parse_iso


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
            ts = session.get("created_at") or session.get("started_at") or session.get("start")
            if not isinstance(ts, str):
                continue
            st = parse_iso(ts)
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
