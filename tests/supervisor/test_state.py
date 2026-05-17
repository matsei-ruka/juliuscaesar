"""Tests for supervisor state load/save."""

from supervisor.state import EventState, SupervisorState


def test_roundtrip(tmp_path):
    state = SupervisorState()
    ev = state.event(42)
    ev.last_phase = "coding"
    ev.card_count = 3
    ev.language = "it"
    state.last_tick_at = 1234567890.0
    state.save(tmp_path)

    loaded = SupervisorState.load(tmp_path)
    assert loaded.last_tick_at == 1234567890.0
    ev2 = loaded.events["42"]
    assert ev2.last_phase == "coding"
    assert ev2.card_count == 3
    assert ev2.language == "it"


def test_load_missing_returns_empty(tmp_path):
    state = SupervisorState.load(tmp_path)
    assert state.last_tick_at == 0.0
    assert state.events == {}


def test_prune_removes_stale(tmp_path):
    state = SupervisorState()
    state.event(1).last_phase = "reading"
    state.event(2).last_phase = "coding"
    state.event(3).last_phase = "thinking"
    state.prune({2})  # keep only event 2
    assert "1" not in state.events
    assert "2" in state.events
    assert "3" not in state.events


def test_atomic_write_uses_tmp(tmp_path):
    state = SupervisorState()
    state.event(99).last_phase = "idle"
    state.save(tmp_path)
    # No .tmp file should remain after save
    state_dir = tmp_path / "state" / "supervisor"
    tmp_files = list(state_dir.glob("*.tmp"))
    assert tmp_files == []


def test_load_corrupt_json_returns_empty(tmp_path):
    path = tmp_path / "state" / "supervisor"
    path.mkdir(parents=True)
    (path / "state.json").write_text("not json {{{")
    state = SupervisorState.load(tmp_path)
    assert state.events == {}


def test_event_creates_default(tmp_path):
    state = SupervisorState()
    ev = state.event(7)
    assert isinstance(ev, EventState)
    assert ev.card_count == 0
    assert ev.recovery_attempts == 0
