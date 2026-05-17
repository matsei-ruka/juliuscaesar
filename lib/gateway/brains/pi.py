"""pi.dev brain wrapper."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..config import env_value
from ..queue import Event
from .base import Brain

# pi session filename format: <ISO-timestamp>_<uuid>.jsonl
# e.g. 2026-05-14T13-28-21-813Z_019e26ac-8834-7582-93d5-e2aec599fe45.jsonl
# Path.stem strips the extension, so match against the bare stem.
_PI_SESSION_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _session_has_image_url(path: Path) -> bool:
    """Return True if the pi session file contains image content blocks.

    Pi stores images as {"type":"image","mimeType":"...","data":"..."} in its
    session JSONL. When serializing for the DeepSeek API, pi converts these to
    {"type":"image_url",...} — the form flash rejects with 400. Detect the
    stored form ("type":"image") so we can upgrade to a vision model before
    resuming such sessions. Byte-string search is fast even on multi-MB files.
    """
    try:
        return b'"type":"image"' in path.read_bytes()
    except OSError:
        return False


def _pi_session_dir(cwd: str) -> Path:
    """Return the pi session directory for the given cwd.

    pi derives session directories from the real (symlink-resolved) cwd:
        slug = '--' + realpath(cwd).lstrip('/').replace('/', '-') + '--'
    Sessions live under ~/.pi/agent/sessions/<slug>/.

    Formula confirmed against pi v0.74.0 on macOS.
    """
    real = os.path.realpath(cwd)
    slug = "--" + real.lstrip("/").replace("/", "-") + "--"
    return Path.home() / ".pi" / "agent" / "sessions" / slug


def _snapshot_session_paths(root: Path) -> frozenset[str]:
    """Snapshot all session JSONL paths under root.

    Matches CodexBrain._snapshot_session_paths pattern. Used by
    pre_invoke_snapshot/capture_session_id for safe set-difference
    session identification.
    """
    if not root.is_dir():
        return frozenset()
    try:
        return frozenset(str(p) for p in root.rglob("*.jsonl"))
    except OSError:
        return frozenset()


class PiBrain(Brain):
    """Gateway brain wrapping pi.dev's `pi -p` print mode.

    Invokes pi as a subprocess, feeds the full gateway preamble via stdin,
    captures the session UUID for multi-turn resume, and injects the
    gateway output contract so pi emits structured JSON.
    """

    name = "pi"
    needs_l1_preamble = True

    # ------------------------------------------------------------------
    # Brain override config accessors
    # ------------------------------------------------------------------

    @property
    def _no_tools(self) -> bool:
        """Read no_tools from brain override config. Default: False (tools on).

        Mirrors claude/codex parity: pi is an agentic CLI; gateway brain defaults
        to tools-enabled. Set ``brains.pi.no_tools: true`` in gateway.yaml to
        disable for a pure chat-only persona.
        """
        raw = getattr(self.override, "no_tools", None)
        if raw is None:
            return False
        return bool(raw)

    @property
    def _thinking(self) -> str | None:
        """Read thinking level from brain override config. Default: None."""
        raw = getattr(self.override, "thinking", None)
        if raw and str(raw).strip():
            return str(raw).strip()
        return None

    @property
    def _vision_model(self) -> str | None:
        """Model to use when session contains image_url content.

        Set ``brains.pi.vision_model: <model>`` in gateway.yaml (model only,
        no ``pi:`` prefix). When unset, no automatic upgrade occurs.
        """
        raw = getattr(self.override, "vision_model", None)
        if raw and str(raw).strip():
            return str(raw).strip()
        return None

    # ------------------------------------------------------------------
    # Brain hooks
    # ------------------------------------------------------------------

    def extra_env(self) -> dict[str, str]:
        """Inject API keys from instance .env and tools toggle.

        pi reads auth from ~/.pi/auth.json (OAuth) or environment
        variables. The gateway starts with env -i, so os.environ won't
        have the operator's keys. Inject them so the pi subprocess picks
        them up. Never pass credentials on the command line.
        """
        env: dict[str, str] = {}
        for key_name in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "DEEPSEEK_API_KEY",
            "GROQ_API_KEY",
            "OPENROUTER_API_KEY",
        ):
            key_value = env_value(self.instance_dir, key_name)
            if key_value:
                env[key_name] = key_value
        # Signal the adapter to enable/disable tools.
        env["JC_PI_NO_TOOLS"] = "1" if self._no_tools else "0"
        return env

    def extra_args_for_event(self, event: Event) -> tuple[str, ...]:
        """Pass --thinking and image paths to the adapter.

        Image paths are extracted from event metadata and forwarded as
        --image /path pairs.  pi.sh converts these to @/path (pi's file-
        attachment syntax) before invoking pi.
        """
        meta = self._meta(event)
        args: list[str] = []
        thinking = self._thinking
        if thinking:
            args.extend(["--thinking", thinking])
        for key in ("image_path", "image"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                args.extend(["--image", value.strip()])
        image_paths = meta.get("image_paths")
        if isinstance(image_paths, list):
            for p in image_paths:
                if isinstance(p, str) and p.strip():
                    args.extend(["--image", p.strip()])
        return tuple(args)

    def adjust_model(self, model: str | None, resume_session: str | None) -> str | None:
        """Upgrade to vision_model when the current event has an image attachment.

        Session-based upgrade is now handled by adjust_resume_session (which
        drops contaminated sessions). This hook handles the case where a
        non-image triage class was selected despite the event carrying an
        image attachment.
        """
        return model

    def adjust_resume_session(
        self, model: str | None, resume_session: str | None
    ) -> str | None:
        """Drop pi sessions that contain image content blocks.

        DeepSeek flash AND pro both reject the image_url wire format that pi
        generates when replaying stored image blocks from a resumed session
        (400: unknown variant 'image_url', expected 'text'). The only safe
        recovery is to start a fresh session, losing image history but
        avoiding a crash loop.
        """
        if resume_session is None:
            return None
        session_file = self._find_session_file(resume_session)
        if session_file and _session_has_image_url(session_file):
            return None
        return resume_session

    def _find_session_file(self, session_uuid: str) -> Path | None:
        """Locate the pi session JSONL for the given UUID, or None."""
        root = _pi_session_dir(str(self.instance_dir))
        if not root.is_dir():
            return None
        matches = list(root.glob(f"*{session_uuid}*"))
        return matches[0] if matches else None

    # Note: gateway output contract is injected by the adapter via
    # --append-system-prompt (mirrors claude.sh). No prompt_for_event override
    # needed; the base class preamble (L1 memory, clock, metadata, voice,
    # chats) is sufficient.

    # ------------------------------------------------------------------
    # Session capture (matches CodexBrain pre/post snapshot pattern)
    # ------------------------------------------------------------------

    def pre_invoke_snapshot(self) -> frozenset[str]:
        """Snapshot session JSONL paths before pi -p spawns.

        Brain.invoke() stores the return value on self._pre_state before
        spawning the adapter, making it available to capture_session_id
        for safe set-difference identification.
        """
        return _snapshot_session_paths(
            _pi_session_dir(str(self.instance_dir))
        )

    def capture_session_id(self, started_at: str) -> str | None:
        """Return the session UUID created by this invocation, or None.

        Diffs pre/post snapshots of the pi session directory. Returns the
        UUID from the new filename stem. Returns None when:

        - No new session file was created (adapter failed, or pi chose
          not to write one).
        - More than one new file appeared (concurrent pi activity created
          ambiguous state — safer to let the next turn fall back to
          transcript priming).
        - The new filename doesn't match the <ts>_<uuid>.jsonl pattern.
        """
        before = getattr(self, "_pre_state", None)
        if not isinstance(before, frozenset):
            before = frozenset()
        after = _snapshot_session_paths(
            _pi_session_dir(str(self.instance_dir))
        )
        new_paths = after - before
        if not new_paths or len(new_paths) > 1:
            return None
        stem = Path(next(iter(new_paths))).stem
        match = _PI_SESSION_UUID_RE.search(stem)
        return match.group(0) if match else None
