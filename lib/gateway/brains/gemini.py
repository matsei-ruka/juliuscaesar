"""Gemini brain wrapper."""

from __future__ import annotations

import subprocess

from .base import Brain, UUID_RE


class GeminiBrain(Brain):
    name = "gemini"

    def extra_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.override.yolo:
            env["GEMINI_YOLO"] = "1"
        return env

    def capture_session_id(self, started_at: str) -> str | None:
        try:
            proc = subprocess.run(
                ["gemini", "--list-sessions"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        matches = UUID_RE.findall(proc.stdout)
        return matches[-1] if matches else None
