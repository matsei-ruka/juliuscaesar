"""Card renderer — pure functions, no I/O.

The card is the user-facing progress signal:

    <emoji> <title>

    Fase: <phase_label>
    Attività: <activity_bar>  <freshness_note>
    Ultimo segnale: <narration>
    Tempo: <elapsed_mmss>

Renders in the request's language (EN/IT). Activity bar is 10 unicode blocks
driven by stderr mtime — full bar = active within 1s, decays linearly to
empty over 60s. Narration is optional (Phase 1 leaves it blank; Phase 3
wires the AI narrator).
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import PhaseResult


_BAR_WIDTH = 10
_BAR_DECAY_SECONDS = 60.0


_LABELS: dict[str, dict[str, str]] = {
    "phase":      {"en": "Phase",      "it": "Fase"},
    "activity":   {"en": "Activity",   "it": "Attività"},
    "signal":     {"en": "Last signal","it": "Ultimo segnale"},
    "time":       {"en": "Elapsed",    "it": "Tempo"},
    "ago_seconds":{"en": "last output {s}s ago",
                   "it": "ultimo output {s}s fa"},
    "ago_never":  {"en": "no output yet",
                   "it": "nessun output ancora"},
}


@dataclass(frozen=True)
class Card:
    """A rendered supervisor card ready for channel delivery."""
    text: str
    phase: str
    emoji: str
    language: str


def render_card(
    *,
    title: str,
    phase: PhaseResult,
    elapsed_seconds: float,
    narration: str = "",
    language: str = "en",
) -> Card:
    """Build the card text. Pure function; no I/O."""
    lang = language if language in ("en", "it") else "en"
    title_short = _truncate(title, 60)

    elapsed = _format_elapsed(elapsed_seconds)

    lines = [
        f"{phase.emoji} {title_short}",
        "",
    ]
    if narration:
        lines.append(f"{_LABELS['signal'][lang]}: {narration}")
    lines.append(f"{_LABELS['time'][lang]}: {elapsed}")

    return Card(
        text="\n".join(lines),
        phase=phase.phase,
        emoji=phase.emoji,
        language=lang,
    )


def render_final_card(
    *,
    title: str,
    elapsed_seconds: float,
    language: str = "en",
) -> Card:
    """Card shown after the event completes successfully (✅ replacement)."""
    lang = language if language in ("en", "it") else "en"
    title_short = _truncate(title, 60)
    elapsed = _format_elapsed(elapsed_seconds)
    done_label = "completato" if lang == "it" else "done"
    text = f"✅ {title_short}\n\n{_LABELS['time'][lang]}: {elapsed} · {done_label}"
    return Card(text=text, phase="done", emoji="✅", language=lang)


def render_stopped_card(
    *,
    title: str,
    elapsed_seconds: float,
    language: str = "en",
) -> Card:
    """Neutral terminal card for failed/escalated events.

    Per the loop-guard / no-crash-exposure spec, the user must never see
    "crash" or "error" text — the supervisor either silently recovers or
    quietly closes the card. This renderer is used by ``_finalize_completed``
    for ``status='failed'`` rows and by the escalation path (Bugs #10, #11).
    """
    lang = language if language in ("en", "it") else "en"
    title_short = _truncate(title, 60)
    elapsed = _format_elapsed(elapsed_seconds)
    stopped_label = "interrotto" if lang == "it" else "stopped"
    text = f"⏹ {title_short}\n\n{_LABELS['time'][lang]}: {elapsed} · {stopped_label}"
    return Card(text=text, phase="stopped", emoji="⏹", language=lang)


def _activity_bar(activity_age_seconds: float | None) -> str:
    """10-block bar; full = active within 1s, empties linearly over 60s."""
    if activity_age_seconds is None:
        return "░" * _BAR_WIDTH
    age = max(0.0, activity_age_seconds)
    if age <= 1.0:
        filled = _BAR_WIDTH
    elif age >= _BAR_DECAY_SECONDS:
        filled = 0
    else:
        # Linear decay between 1s (full) and 60s (empty)
        ratio = 1.0 - (age - 1.0) / (_BAR_DECAY_SECONDS - 1.0)
        filled = max(0, min(_BAR_WIDTH, int(round(ratio * _BAR_WIDTH))))
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def _freshness_note(activity_age_seconds: float | None, lang: str) -> str:
    if activity_age_seconds is None:
        return _LABELS["ago_never"][lang]
    s = max(0, int(round(activity_age_seconds)))
    return _LABELS["ago_seconds"][lang].format(s=s)


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes = total // 60
    secs = total % 60
    return f"{minutes:02d}:{secs:02d}"


def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    s = text.strip().replace("\n", " ")
    if len(s) <= max_chars:
        return s
    # Truncate on word boundary if possible
    cut = s[:max_chars].rsplit(" ", 1)[0]
    if not cut:
        cut = s[:max_chars]
    return cut + "…"
