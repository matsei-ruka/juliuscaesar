"""Tests for detector.py — pattern detection."""

from pathlib import Path

import pytest

from lib.user_model.corpus import Event
from lib.user_model.detector import (
    Signal,
    detect_all,
    _detect_recurring_topics,
    _detect_new_entities,
)
from lib.user_model.conf import UserModelConfig


class TestDetectRecurringTopics:
    def test_detects_frequent_terms(self):
        events = [
            Event(1, "user_1", "conv_1", "Martina is super smart today", None, "2026-04-28T10:00:00Z"),
            Event(2, "user_1", "conv_2", "Martina did great in class", None, "2026-04-28T11:00:00Z"),
            Event(3, "user_1", "conv_3", "Martina loves coding", None, "2026-04-28T12:00:00Z"),
        ]
        config = UserModelConfig(min_evidence_count=2)
        user_md = "## Family\n- Daughter: Jane"  # Martina not mentioned

        signals = list(_detect_recurring_topics(events, config, user_md))
        assert len(signals) > 0
        # "martina" should be detected (appears 3 times, across 3 conversations)
        martina_signals = [s for s in signals if s.term == "martina"]
        assert len(martina_signals) > 0

    def test_skips_already_mentioned_terms(self):
        events = [
            Event(1, "user_1", "conv_1", "Luca is working today", None, "2026-04-28T10:00:00Z"),
            Event(2, "user_1", "conv_2", "Luca is amazing", None, "2026-04-28T11:00:00Z"),
            Event(3, "user_1", "conv_3", "Luca met the team", None, "2026-04-28T12:00:00Z"),
        ]
        config = UserModelConfig(min_evidence_count=2)
        user_md = "## Role\n- Name: Luca"  # Luca already mentioned

        signals = list(_detect_recurring_topics(events, config, user_md))
        # "luca" should NOT be detected (already in user_md)
        luca_signals = [s for s in signals if s.term == "luca"]
        assert len(luca_signals) == 0

    def test_requires_multiple_conversations(self):
        events = [
            Event(1, "user_1", "conv_1", "Martina is smart Martina is smart", None, "2026-04-28T10:00:00Z"),
        ]
        config = UserModelConfig(min_evidence_count=2)
        user_md = "## Family"

        signals = list(_detect_recurring_topics(events, config, user_md))
        # "martina" should NOT be detected (only 1 conversation, threshold is 2)
        martina_signals = [s for s in signals if s.term == "martina"]
        assert len(martina_signals) == 0


class TestDetectNewEntities:
    def test_detects_capitalized_entities(self):
        events = [
            Event(1, "user_1", "conv_1", "Dove Green school is great", None, "2026-04-28T10:00:00Z"),
            Event(2, "user_1", "conv_2", "Dove Green has amazing teachers", None, "2026-04-28T11:00:00Z"),
            Event(3, "user_1", "conv_3", "Dove Green curriculum is solid", None, "2026-04-28T12:00:00Z"),
        ]
        config = UserModelConfig(min_evidence_count=3)
        user_md = "## Education"

        signals = list(_detect_new_entities(events, config, user_md))
        # "Dove Green" should be detected (3 times)
        dg_signals = [s for s in signals if "Dove" in (s.term or "")]
        assert len(dg_signals) > 0

    def test_skips_existing_entities(self):
        events = [
            Event(1, "user_1", "conv_1", "Martina loves Dove Green", None, "2026-04-28T10:00:00Z"),
            Event(2, "user_1", "conv_2", "Martina is at Dove Green", None, "2026-04-28T11:00:00Z"),
            Event(3, "user_1", "conv_3", "Martina from Dove Green", None, "2026-04-28T12:00:00Z"),
        ]
        config = UserModelConfig(min_evidence_count=3)
        user_md = "## Family\n- Daughter [[people/martina|Martina]]"  # Martina exists as wikilink

        signals = list(_detect_new_entities(events, config, user_md))
        # "Martina" should NOT be detected (already a wikilink)
        martina_signals = [s for s in signals if s.term == "Martina"]
        assert len(martina_signals) == 0


class TestDetectAll:
    def test_runs_all_enabled_detectors(self):
        events = [
            Event(1, "user_1", "conv_1", "Martina is smart", None, "2026-04-28T10:00:00Z"),
            Event(2, "user_1", "conv_2", "Martina loves coding", None, "2026-04-28T11:00:00Z"),
            Event(3, "user_1", "conv_3", "Martina super awesome", None, "2026-04-28T12:00:00Z"),
        ]
        config = UserModelConfig(min_evidence_count=2)
        user_md = "## Family"

        signals = list(detect_all(Path("/tmp"), events, config, user_md))
        # Should have multiple signals from different detectors
        assert len(signals) > 0
        kinds = {s.kind for s in signals}
        assert "recurring_topic" in kinds or "new_entity" in kinds
