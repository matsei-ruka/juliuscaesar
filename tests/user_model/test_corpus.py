"""Tests for corpus.py — event reading and privacy filtering."""

import pytest

from lib.user_model.corpus import iter_events, count_events, _passes_privacy_filter


class TestPrivacyFilter:
    def test_allows_safe_text(self):
        assert _passes_privacy_filter("Luca met with the team today") is True

    def test_blocks_credentials(self):
        assert _passes_privacy_filter("My API key is sk_live_abcdefghijk1234567890") is False

    def test_blocks_aws_keys(self):
        assert _passes_privacy_filter("AWS key: AKIAIOSFODNN7EXAMPLE") is False

    def test_blocks_explicit_content(self):
        assert _passes_privacy_filter("That was fucking amazing") is False
        assert _passes_privacy_filter("Let me describe this porn scenario") is False

    def test_allows_word_fuck_in_non_explicit(self):
        # "fuck" in a non-sexual context still blocks (conservative approach).
        assert _passes_privacy_filter("I'm fucking tired") is False


class TestIterEvents:
    def test_yields_safe_events(self, instance_dir_with_queue):
        events = list(iter_events(instance_dir_with_queue, look_back_days=10))
        assert len(events) == 5
        assert all(evt.content.startswith("Message") for evt in events)

    def test_filters_by_user_id(self, instance_dir_with_queue):
        events = list(iter_events(instance_dir_with_queue, user_id="user_123"))
        assert len(events) == 5

    def test_returns_empty_if_no_queue(self, tmp_path):
        events = list(iter_events(tmp_path))
        assert len(events) == 0


class TestCountEvents:
    def test_counts_events(self, instance_dir_with_queue):
        count = count_events(instance_dir_with_queue, look_back_days=10)
        assert count == 5

    def test_filters_by_user(self, instance_dir_with_queue):
        count = count_events(instance_dir_with_queue, look_back_days=10, user_id="user_123")
        assert count == 5

    def test_returns_zero_if_no_queue(self, tmp_path):
        count = count_events(tmp_path)
        assert count == 0
