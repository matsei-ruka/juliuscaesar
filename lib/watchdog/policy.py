"""Restart policy — backoff calculation + restart-budget enforcement."""

from __future__ import annotations

import time

from .child import ChildSpec, ChildState


def backoff_for(spec: ChildSpec, state: ChildState) -> int:
    """Return the delay (seconds) to wait before the next restart attempt."""
    table = spec.restart.backoff or (5,)
    idx = max(0, state.consecutive_failures - 1)
    if idx >= len(table):
        idx = len(table) - 1
    return int(table[idx])


def trim_window(spec: ChildSpec, state: ChildState, *, now: float | None = None) -> None:
    """Drop attempts older than the policy window from `state.attempts_in_window`."""
    now = now or time.time()
    cutoff = now - spec.restart.window_seconds
    state.attempts_in_window = [t for t in state.attempts_in_window if t >= cutoff]


def may_restart(spec: ChildSpec, state: ChildState, *, now: float | None = None) -> bool:
    """True iff a new restart is permitted under the budget."""
    trim_window(spec, state, now=now)
    return len(state.attempts_in_window) < spec.restart.max_in_window


def record_attempt(state: ChildState, *, now: float | None = None) -> None:
    now = now or time.time()
    state.attempts_in_window.append(now)
    state.last_attempt_at = now
    state.consecutive_failures += 1


def record_started(state: ChildState, *, now: float | None = None) -> None:
    state.last_started_at = now or time.time()


def record_healthy(state: ChildState) -> None:
    state.consecutive_failures = 0
    # Keep attempts_in_window as-is — the budget is over a sliding window,
    # not per success, so a flapping child eventually trips alert mode even
    # if some restarts briefly look healthy.


def attempt_due(spec: ChildSpec, state: ChildState, *, now: float | None = None) -> bool:
    """True iff enough time has elapsed since the last attempt for the next try."""
    if state.consecutive_failures <= 0:
        return True
    delay = backoff_for(spec, state)
    return ((now or time.time()) - state.last_attempt_at) >= delay
