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
from typing import Any

from .models import PhaseResult


_BAR_WIDTH = 10
_BAR_DECAY_SECONDS = 60.0

_MINUTE_COLORS = ("🟦", "🟩", "🟨", "🟧", "🟥", "🟪", "⬛")
_MINUTES_PER_COLOR = 3

_SLOT_EMOJIS = (
    "0️⃣",
    "1️⃣",
    "2️⃣",
    "3️⃣",
    "4️⃣",
    "5️⃣",
    "6️⃣",
    "7️⃣",
    "8️⃣",
    "9️⃣",
)


def _slot_emoji(n: int) -> str:
    """Return the keycap emoji for slot N (0-9). Caps at 9."""
    idx = max(0, min(9, int(n)))
    return _SLOT_EMOJIS[idx]


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
    """A rendered supervisor card ready for channel delivery.

    ``reply_markup`` and ``short_token`` are populated only when supervisor
    card actions (`gateway.actions.enabled: true`) are on. Delivery layers
    that support inline keyboards (Telegram today) attach the markup to the
    outbound payload and bind ``short_token`` → ``supervisor_msg_id`` in the
    action registry after a successful send. Channels without inline-keyboard
    support ignore both fields.
    """
    text: str
    phase: str
    emoji: str
    language: str
    reply_markup: dict[str, Any] | None = None
    short_token: str | None = None


def build_action_keyboard(short_token: str) -> dict[str, Any]:
    """Inline keyboard for supervisor card actions: ✋ Stop | 🔄 Background.

    callback_data format: ``act:<verb>:<short_token>`` where ``<short_token>``
    is the first 12 chars of the action session UUID (hex). Bounded well
    under Telegram's 64-byte callback_data limit.
    """
    return {
        "inline_keyboard": [[
            {
                "text": "✋ Stop",
                "callback_data": f"act:stop:{short_token}",
            },
            {
                "text": "🔄 Background",
                "callback_data": f"act:bg:{short_token}",
            },
        ]]
    }


def build_action_components_discord(short_token: str) -> list[dict[str, Any]]:
    """Discord message components for supervisor card actions: Stop | Background.

    Returns a single action row (component type 1) holding two buttons
    (component type 2). The ``custom_id`` reuses the exact token contract the
    Telegram inline keyboard uses (``act:<verb>:<short_token>``) so one
    interaction parser serves both channels. Button styles: 4 = danger (red)
    for Stop, 2 = secondary (grey) for Background.
    """
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 4,
                    "label": "✋ Stop",
                    "custom_id": f"act:stop:{short_token}",
                },
                {
                    "type": 2,
                    "style": 2,
                    "label": "🔄 Background",
                    "custom_id": f"act:bg:{short_token}",
                },
            ],
        }
    ]


def render_card(
    *,
    title: str,
    phase: PhaseResult,
    elapsed_seconds: float,
    narration: str = "",
    language: str = "en",
    slot: int | None = None,
    max_concurrent: int = 1,
    actions_short_token: str | None = None,
) -> Card:
    """Build the card text. Pure function; no I/O.

    When `slot` is provided AND `max_concurrent > 1`, the leading emoji is the
    slot keycap (`0️⃣`..`9️⃣`) and the phase emoji moves to a second line. This
    keeps multiple parallel cards in the same conversation visually
    distinguishable. For `max_concurrent <= 1` (default), behavior is
    identical to the pre-parallel-slots renderer.
    """
    lang = language if language in ("en", "it") else "en"
    title_short = _truncate(title, 60)

    elapsed_line = _minute_bar(elapsed_seconds)

    show_slot = slot is not None and max_concurrent > 1

    if show_slot:
        head_emoji = _slot_emoji(int(slot))  # type: ignore[arg-type]
        lines = [
            f"{head_emoji} {title_short}",
            "",
        ]
        # Phase emoji on line 2, embedded in the activity line.
        activity_bits: list[str] = [phase.emoji]
        if narration:
            activity_bits.append(f"{_LABELS['signal'][lang]}: {narration}")
        activity_bits.append(elapsed_line)
        lines.append(" · ".join(activity_bits))
    else:
        lines = [
            f"{phase.emoji} {title_short}",
            "",
        ]
        if narration:
            lines.append(f"{_LABELS['signal'][lang]}: {narration}")
        lines.append(elapsed_line)

    reply_markup = (
        build_action_keyboard(actions_short_token) if actions_short_token else None
    )
    return Card(
        text="\n".join(lines),
        phase=phase.phase,
        emoji=phase.emoji,
        language=lang,
        reply_markup=reply_markup,
        short_token=actions_short_token,
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
    elapsed_line = _minute_bar(elapsed_seconds)
    done_label = "completato" if lang == "it" else "done"
    text = f"✅ {title_short}\n\n{elapsed_line} · {done_label}"
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
    elapsed_line = _minute_bar(elapsed_seconds)
    stopped_label = "interrotto" if lang == "it" else "stopped"
    text = f"⏹ {title_short}\n\n{elapsed_line} · {stopped_label}"
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


def _minute_bar(seconds: float) -> str:
    """Colored square per elapsed minute + "(N min elapsed)" text.

    Colors cycle every 3 minutes through {blue, green, yellow, orange, red,
    purple, black} for visual chunking. Output: "🟦🟦🟦🟩🟩🟩 (6 min elapsed)".
    Wraps back to blue after black (21 min).
    """
    minutes = max(0, int(seconds)) // 60
    if minutes == 0:
        return "(0 min elapsed)"
    squares = "".join(
        _MINUTE_COLORS[(i // _MINUTES_PER_COLOR) % len(_MINUTE_COLORS)]
        for i in range(minutes)
    )
    return f"{squares} ({minutes} min elapsed)"


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
