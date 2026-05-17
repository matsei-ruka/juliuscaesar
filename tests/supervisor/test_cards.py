"""Tests for the supervisor card renderer."""

from supervisor.cards import Card, render_card, render_final_card
from supervisor.models import PhaseResult


def _phase(name="coding", emoji="🛠️", labels=None):
    if labels is None:
        labels = {"en": "coding", "it": "sviluppo"}
    return PhaseResult(phase=name, emoji=emoji, label=labels)


def test_render_card_returns_card_dataclass():
    card = render_card(
        title="audit Athena repo",
        phase=_phase(),
        elapsed_seconds=130.0,
        language="en",
    )
    assert isinstance(card, Card)
    assert card.phase == "coding"
    assert card.emoji == "🛠️"
    assert card.language == "en"


def test_card_contains_emoji_and_title():
    card = render_card(
        title="audit Athena repo",
        phase=_phase(),
        elapsed_seconds=10.0,
        language="en",
    )
    assert "🛠️" in card.text
    assert "audit Athena repo" in card.text


def test_card_no_phase_or_activity_lines():
    # Phase and activity bar lines have been removed from card format.
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=10.0,
        language="en",
    )
    assert "Phase:" not in card.text
    assert "Activity:" not in card.text
    assert "Fase:" not in card.text
    assert "Attività:" not in card.text


def test_card_elapsed_minute_bar_zero():
    """Below 1 min: just '(0 min)' text, no squares."""
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=10.0,
        language="en",
    )
    assert "(0 min)" in card.text


def test_card_elapsed_label_italian_same_format():
    """Minute bar is language-agnostic ('min' works in EN+IT)."""
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=10.0,
        language="it",
    )
    assert "(0 min)" in card.text
    assert "Tempo:" not in card.text


def test_card_includes_narration_when_present():
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=10.0,
        narration="found 184 PHP files",
        language="en",
    )
    assert "found 184 PHP files" in card.text
    assert "Last signal" in card.text


def test_card_omits_signal_line_when_narration_empty():
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=10.0,
        narration="",
        language="en",
    )
    assert "Last signal" not in card.text


def test_elapsed_minute_bar_with_squares():
    """130s → 2 min → 2 green squares + '(2 min)'."""
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=130.0,
        language="en",
    )
    assert "🟩🟩 (2 min)" in card.text


def test_elapsed_minute_bar_color_cycle():
    """18 min cycles all 6 colors (3 squares each)."""
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=18 * 60.0,
        language="en",
    )
    expected = "🟩🟩🟩🟦🟦🟦🟪🟪🟪🟧🟧🟧🟥🟥🟥🟨🟨🟨 (18 min)"
    assert expected in card.text


def test_elapsed_minute_bar_wraps_after_full_cycle():
    """19 min → 18 colored + 1 green (cycle restarts)."""
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=19 * 60.0,
        language="en",
    )
    assert "🟨🟩 (19 min)" in card.text


def test_title_truncated_to_60_chars():
    long_title = "a" * 100
    card = render_card(
        title=long_title,
        phase=_phase(),
        elapsed_seconds=10.0,
        language="en",
    )
    title_line = card.text.split("\n")[0]
    assert "…" in title_line


def test_title_word_boundary_truncate():
    title = "scan all controllers and identify auth routes for security review pass"
    card = render_card(
        title=title,
        phase=_phase(),
        elapsed_seconds=10.0,
        language="en",
    )
    first = card.text.split("\n")[0]
    assert first.endswith("…")


def test_unknown_language_falls_back_to_english():
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=10.0,
        language="de",
    )
    assert card.language == "en"
    assert "(0 min)" in card.text


def test_final_card_has_check_emoji():
    card = render_final_card(title="audit done", elapsed_seconds=180.0, language="en")
    assert card.emoji == "✅"
    assert card.phase == "done"
    assert "✅" in card.text


def test_final_card_italian():
    card = render_final_card(title="audit done", elapsed_seconds=180.0, language="it")
    assert "completato" in card.text
    assert "🟩🟩🟩 (3 min)" in card.text


def test_final_card_english():
    card = render_final_card(title="audit done", elapsed_seconds=180.0, language="en")
    assert "done" in card.text
    assert "🟩🟩🟩 (3 min)" in card.text
