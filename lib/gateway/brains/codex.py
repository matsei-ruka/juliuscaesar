"""Codex brain wrapper (`codex exec`)."""

from __future__ import annotations

from pathlib import Path

from ..queue import Event
from .base import Brain, UUID_RE, newest_jsonl_stem, parse_iso


class CodexBrain(Brain):
    name = "codex"

    def extra_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.override.yolo:
            env["CODEX_SANDBOX"] = "yolo"
        elif self.override.sandbox:
            env["CODEX_SANDBOX"] = str(self.override.sandbox)
        else:
            env["CODEX_SANDBOX"] = "read-only"
        return env

    def extra_args_for_event(self, event: Event) -> tuple[str, ...]:
        meta = self._meta(event)
        paths: list[str] = []
        for key in ("image_path", "image"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value.strip())
        value = meta.get("image_paths")
        if isinstance(value, list):
            paths.extend(str(item).strip() for item in value if str(item).strip())
        args: list[str] = []
        for path in paths:
            args.extend(["--image", path])
        return tuple(args)

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
