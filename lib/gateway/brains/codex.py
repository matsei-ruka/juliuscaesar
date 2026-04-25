"""Codex brain wrapper (`codex exec`)."""

from __future__ import annotations

from pathlib import Path

from .base import Brain, UUID_RE, newest_jsonl_stem, parse_iso


class CodexBrain(Brain):
    name = "codex"

    def extra_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.override.sandbox:
            env["CODEX_SANDBOX"] = str(self.override.sandbox)
        return env

    def capture_session_id(self, started_at: str) -> str | None:
        t0 = parse_iso(started_at)
        if t0 is None:
            return None
        root = Path.home() / ".codex"
        root = root / "sessions" if (root / "sessions").is_dir() else root
        stem = newest_jsonl_stem(root, t0, recursive=True)
        if not stem:
            return None
        match = UUID_RE.search(stem)
        return match.group(0) if match else stem
