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
        activity_age_seconds=8.0,
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
        activity_age_seconds=2.0,
        language="en",
    )
    assert "🛠️" in card.text
    assert "audit Athena repo" in card.text


def test_card_english_labels():
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=2.0,
        language="en",
    )
    assert "Phase:" in card.text
    assert "Activity:" in card.text
    assert "Elapsed:" in card.text


def test_card_italian_labels():
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=2.0,
        language="it",
    )
    assert "Fase:" in card.text
    assert "Attività:" in card.text
    assert "Tempo:" in card.text


def test_card_uses_phase_label_for_language():
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=2.0,
        language="it",
    )
    assert "sviluppo" in card.text


def test_card_includes_narration_when_present():
    card = render_card(
        title="x",
        phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=2.0,
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
        activity_age_seconds=2.0,
        narration="",
        language="en",
    )
    assert "Last signal" not in card.text


def test_activity_bar_full_when_fresh():
    card = render_card(
        title="x", phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=0.5,
        language="en",
    )
    assert "██████████" in card.text


def test_activity_bar_empty_when_stale():
    card = render_card(
        title="x", phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=60.0,
        language="en",
    )
    assert "░░░░░░░░░░" in card.text


def test_activity_bar_partial_decay():
    card = render_card(
        title="x", phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=30.0,
        language="en",
    )
    # Mix of full and decay blocks
    assert "█" in card.text
    assert "░" in card.text


def test_freshness_note_includes_seconds():
    card = render_card(
        title="x", phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=8.0,
        language="en",
    )
    assert "8s ago" in card.text


def test_freshness_note_italian():
    card = render_card(
        title="x", phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=8.0,
        language="it",
    )
    assert "8s fa" in card.text


def test_freshness_none_when_no_mtime():
    card_en = render_card(
        title="x", phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=None,
        language="en",
    )
    assert "no output" in card_en.text
    card_it = render_card(
        title="x", phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=None,
        language="it",
    )
    assert "nessun output" in card_it.text


def test_elapsed_format_mmss():
    card = render_card(
        title="x", phase=_phase(),
        elapsed_seconds=130.0,
        activity_age_seconds=2.0,
        language="en",
    )
    assert "02:10" in card.text


def test_title_truncated_to_60_chars():
    long_title = "a" * 100
    card = render_card(
        title=long_title, phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=2.0,
        language="en",
    )
    title_line = card.text.split("\n")[0]
    # title_line is "🛠️ <title>…" — ellipsis appended on truncation
    assert "…" in title_line


def test_title_word_boundary_truncate():
    title = "scan all controllers and identify auth routes for security review pass"
    card = render_card(
        title=title, phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=2.0,
        language="en",
    )
    first = card.text.split("\n")[0]
    # Should not cut mid-word
    assert first.endswith("…")


def test_unknown_language_falls_back_to_english():
    card = render_card(
        title="x", phase=_phase(),
        elapsed_seconds=10.0,
        activity_age_seconds=2.0,
        language="de",
    )
    assert card.language == "en"
    assert "Phase:" in card.text


def test_final_card_has_check_emoji():
    card = render_final_card(title="audit done", elapsed_seconds=180.0, language="en")
    assert card.emoji == "✅"
    assert card.phase == "done"
    assert "✅" in card.text


def test_final_card_italian():
    card = render_final_card(title="audit done", elapsed_seconds=180.0, language="it")
    assert "completato" in card.text
    assert "Tempo:" in card.text


def test_final_card_english():
    card = render_final_card(title="audit done", elapsed_seconds=180.0, language="en")
    assert "done" in card.text
    assert "Elapsed:" in card.text
