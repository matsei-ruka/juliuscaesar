"""Optional operator footer appended to gateway text replies."""

from __future__ import annotations

from .config import ReplyFooterConfig


def _abbrev_session(session_id: str | None, chars: int) -> str:
    if session_id is None:
        return "none"
    if len(session_id) <= chars:
        return session_id
    return f"{session_id[:chars]}…"


def _format_elapsed(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def render_footer(
    cfg: ReplyFooterConfig,
    *,
    brain: str,
    model: str | None,
    session_id: str | None,
    elapsed_seconds: float | None,
    slot: int | None = None,
    max_concurrent: int = 1,
) -> str | None:
    """Render a single-line reply footer, or None when disabled/empty.

    When `slot is not None` AND `max_concurrent > 1`, a `slot N` segment is
    inserted after `brain` and before `sess`. For `max_concurrent <= 1`
    (default) the slot is suppressed so the footer is byte-identical to the
    pre-parallel-slots output.
    """
    if not cfg.enabled:
        return None

    parts: list[str] = []
    if cfg.show_model and brain:
        parts.append(f"{brain}:{model}" if model else brain)
    if slot is not None and max_concurrent > 1:
        parts.append(f"slot {int(slot)}")
    if cfg.show_session:
        parts.append(f"sess {_abbrev_session(session_id, cfg.session_chars)}")
    if cfg.show_elapsed:
        elapsed = _format_elapsed(elapsed_seconds)
        if elapsed:
            parts.append(elapsed)
    if not parts:
        return None

    prefix = f"{cfg.emoji} " if cfg.emoji else ""
    return prefix + cfg.separator.join(parts)
