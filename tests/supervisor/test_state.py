"""Tests for supervisor state load/save."""

from supervisor.state import RECOVERY_STATE_TTL_SECONDS, EventState, SupervisorState


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


# Bug #1 — recovery counter persists across recover→requeue→reclaim cycle.
def test_prune_pins_entries_with_recovery_attempts(tmp_path):
    """Entries with non-zero recovery_attempts must survive prune while inside
    the TTL window — otherwise the counter resets and escalation never fires
    for flapping events."""
    state = SupervisorState()
    state.event(1).last_phase = "reading"  # no recovery → prunable
    ev2 = state.event(2)
    ev2.recovery_attempts = 1
    state.event(3).escalated = True  # treated like recovery state

    now = 1_000_000.0
    state.prune({99}, now=now)  # nothing active

    # Pristine entry was dropped, recovery + escalated kept and pinned.
    assert "1" not in state.events
    assert "2" in state.events
    assert "3" in state.events
    assert state.events["2"].pinned_until == now + RECOVERY_STATE_TTL_SECONDS
    assert state.events["3"].pinned_until == now + RECOVERY_STATE_TTL_SECONDS


def test_prune_evicts_after_ttl(tmp_path):
    """Pinned entries get dropped once the TTL elapses."""
    state = SupervisorState()
    ev = state.event(7)
    ev.recovery_attempts = 2
    ev.pinned_until = 1_001_000.0  # already-set pin from an earlier prune

    state.prune({}, now=1_000_999.0)  # before pin expires → kept
    assert "7" in state.events

    state.prune({}, now=1_001_500.0)  # after pin expires → dropped
    assert "7" not in state.events


def test_prune_does_not_pin_active_events(tmp_path):
    """An active event with recovery_attempts shouldn't get a pinned_until —
    it's still in the snapshot set so the pin is meaningless and would mask
    bugs where active entries get treated as stale."""
    state = SupervisorState()
    ev = state.event(5)
    ev.recovery_attempts = 1

    state.prune({5}, now=1_000_000.0)
    assert "5" in state.events
    assert state.events["5"].pinned_until == 0.0
