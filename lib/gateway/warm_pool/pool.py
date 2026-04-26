"""PoolManager: keyed registry of long-lived `PoolProcess` members.

Pool key = `(conversation_id, brain, model)`. The dispatcher serializes events
through `dispatch_once`, so a member is never invoked concurrently from the
gateway runtime; the lock here protects pool membership from a parallel
shutdown / idle sweep.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .process import PoolProcess, PoolProcessError


PoolKey = tuple[str, str, str | None]
ProcessFactory = Callable[[PoolKey], PoolProcess]


@dataclass
class PoolStats:
    size: int
    hits: int
    misses: int
    evictions: int


class PoolManager:
    def __init__(
        self,
        factory: ProcessFactory,
        *,
        max_size: int = 20,
        idle_timeout_seconds: float = 300.0,
    ) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self._factory = factory
        self._max_size = max_size
        self._idle_timeout = idle_timeout_seconds
        self._members: dict[PoolKey, PoolProcess] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get_or_create(self, key: PoolKey) -> PoolProcess:
        with self._lock:
            existing = self._members.get(key)
            if existing is not None and existing.healthy and existing.is_alive():
                self._hits += 1
                return existing
            if existing is not None:
                # Stale/dead entry — remove before respawning.
                self._evict_locked(key)
            self._evict_idle_locked(now=time.monotonic())
            self._enforce_capacity_locked()
            member = self._factory(key)
            try:
                member.start()
            except PoolProcessError:
                # Don't keep an unstarted member; let caller fall back.
                raise
            self._members[key] = member
            self._misses += 1
            return member

    def release(self, key: PoolKey) -> None:
        # MVP: pool is sticky; release is a no-op except for stamping last_used,
        # which `invoke()` already does. Kept as a public hook so future phases
        # can add ref-counting or LRU promotion without changing callers.
        with self._lock:
            member = self._members.get(key)
            if member is not None:
                member.last_used = time.monotonic()

    def evict(self, key: PoolKey) -> bool:
        with self._lock:
            return self._evict_locked(key)

    def _evict_locked(self, key: PoolKey) -> bool:
        member = self._members.pop(key, None)
        if member is None:
            return False
        try:
            member.terminate()
        finally:
            self._evictions += 1
        return True

    def evict_idle(self) -> int:
        with self._lock:
            return self._evict_idle_locked(now=time.monotonic())

    def _evict_idle_locked(self, *, now: float) -> int:
        if self._idle_timeout <= 0:
            return 0
        stale = [
            key
            for key, member in self._members.items()
            if (now - member.last_used) > self._idle_timeout
        ]
        for key in stale:
            self._evict_locked(key)
        return len(stale)

    def _enforce_capacity_locked(self) -> None:
        # Make room for one new member if we're already at the cap. LRU by
        # last_used. Run after idle eviction so we don't kick a fresh member
        # while a stale one still squats a slot.
        while len(self._members) >= self._max_size:
            victim = min(self._members.items(), key=lambda kv: kv[1].last_used)
            self._evict_locked(victim[0])

    def shutdown(self, *, grace_seconds: float = 5.0) -> None:
        with self._lock:
            keys = list(self._members.keys())
        deadline = time.monotonic() + grace_seconds
        for key in keys:
            remaining = max(0.0, deadline - time.monotonic())
            with self._lock:
                member = self._members.pop(key, None)
            if member is None:
                continue
            try:
                member.terminate(grace_seconds=min(remaining, 2.0))
            except Exception:  # noqa: BLE001
                pass

    def stats(self) -> PoolStats:
        with self._lock:
            return PoolStats(
                size=len(self._members),
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
            )

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def idle_timeout_seconds(self) -> float:
        return self._idle_timeout

    def __len__(self) -> int:
        with self._lock:
            return len(self._members)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._members
