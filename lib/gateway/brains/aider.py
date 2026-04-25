"""Aider brain wrapper.

Aider's resume model is conversation-history-file based: each session id maps
to a JSON history file under
`<instance>/state/gateway/aider-sessions/<id>.json`.

`session_id == None` starts a fresh aider session (the shell adapter creates a
fresh history file). Otherwise the adapter loads the existing history file.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from .base import Brain


class AiderBrain(Brain):
    name = "aider"

    def extra_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        history_dir = self.instance_dir / "state" / "gateway" / "aider-sessions"
        history_dir.mkdir(parents=True, exist_ok=True)
        env["AIDER_HISTORY_DIR"] = str(history_dir)
        return env

    def capture_session_id(self, started_at: str) -> str | None:
        # Reuse the env-injected session id if the adapter wrote one back to a
        # known marker; otherwise mint a fresh one so subsequent messages can
        # resume by history file path.
        marker = self.instance_dir / "state" / "gateway" / "aider-sessions" / "LAST_SESSION"
        if marker.exists():
            try:
                value = marker.read_text(encoding="utf-8").strip()
                if value:
                    return value
            except OSError:
                pass
        session_id = uuid.uuid4().hex
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(session_id, encoding="utf-8")
        except OSError:
            return None
        return session_id
