"""Codex brain wrapper (`codex exec`)."""

from __future__ import annotations

from pathlib import Path

from ..queue import Event
from .base import Brain, UUID_RE


def _session_root() -> Path:
    """Return the directory Codex writes per-session JSONL into.

    Modern Codex puts these under `~/.codex/sessions/`. Older builds wrote
    directly to `~/.codex/`. Fall back so capture works on both.
    """
    base = Path.home() / ".codex"
    nested = base / "sessions"
    return nested if nested.is_dir() else base


def _snapshot_session_paths(root: Path) -> frozenset[str]:
    if not root.is_dir():
        return frozenset()
    try:
        return frozenset(str(p) for p in root.rglob("*.jsonl"))
    except OSError:
        return frozenset()


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

    def pre_invoke_snapshot(self) -> frozenset[str]:
        """Snapshot session-jsonl paths before `codex exec` spawns.

        Captured pre-spawn so `capture_session_id` can identify the file this
        invocation creates by set-difference. This avoids the timestamp-only
        global scan that could pick up an unrelated session created by a
        concurrent Codex process.
        """
        return _snapshot_session_paths(_session_root())

    def capture_session_id(self, started_at: str) -> str | None:
        """Return the session id created by this invocation, or None.

        Uses pre/post snapshot of `~/.codex/sessions/` so we never resume an
        unrelated session id that just happens to share a timestamp window.
        Returns None when:

        - no new session file was created (e.g. `codex exec` failed or the
          adapter ran without writing a session record), or
        - more than one new session file appeared (concurrent Codex activity
          created ambiguous state — safer to fall back to transcript
          priming on the next turn).

        Per docs/specs/codex-main-brain-hardening.md §Phase 4: never resume a
        session id that cannot be tied to this gateway invocation.
        """
        before = getattr(self, "_pre_state", None)
        if not isinstance(before, frozenset):
            before = frozenset()
        after = _snapshot_session_paths(_session_root())
        new_paths = after - before
        if not new_paths or len(new_paths) > 1:
            return None
        stem = Path(next(iter(new_paths))).stem
        match = UUID_RE.search(stem)
        return match.group(0) if match else stem
