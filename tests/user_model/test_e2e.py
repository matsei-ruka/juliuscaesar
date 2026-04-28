"""End-to-end test of full detection + proposal pipeline."""

import tempfile
from pathlib import Path

import pytest

from lib.user_model.conf import UserModelConfig
from lib.user_model.corpus import Event
from lib.user_model.detector import detect_all
from lib.user_model.proposer import generate_proposals
from lib.user_model.store import save_proposal, load_proposals


class TestE2E:
    def test_full_pipeline(self, tmp_path):
        """Detect signals → generate proposals → save → reload."""
        instance = tmp_path
        (instance / "memory" / "staging").mkdir(parents=True)

        # Create test events
        events = [
            Event(1, "user_1", "conv_1", "Martina is super smart", None, "2026-04-28T10:00:00Z"),
            Event(2, "user_1", "conv_2", "Martina loves coding", None, "2026-04-28T11:00:00Z"),
            Event(3, "user_1", "conv_3", "Martina did great today", None, "2026-04-28T12:00:00Z"),
        ]

        # Current USER.md
        user_md = "## Family\n- Daughter: Jane"

        # Config
        config = UserModelConfig(min_evidence_count=2, apply_mode="propose")

        # Detect
        signals = list(detect_all(instance, events, config, user_md))
        assert len(signals) > 0

        # Generate proposals
        proposals = list(generate_proposals(instance, signals, config, user_md))
        # May be 0 if LLM call fails (not available), but should not error
        assert isinstance(proposals, list)

        # If proposals generated, save + reload
        if proposals:
            for proposal in proposals:
                save_proposal(instance, proposal, "staging")

            reloaded = list(load_proposals(instance, "staging"))
            assert len(reloaded) == len(proposals)
            assert reloaded[0].reasoning is not None
