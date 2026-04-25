"""Tiny LRU cache so identical inbound messages within a TTL skip triage."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict

from .base import TriageResult


class TriageCache:
    def __init__(self, *, ttl_seconds: int = 30, max_entries: int = 256):
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, TriageResult]] = OrderedDict()

    def _key(self, message: str) -> str:
        return hashlib.sha256(message.encode("utf-8", errors="replace")).hexdigest()

    def get(self, message: str) -> TriageResult | None:
        if self.ttl_seconds <= 0:
            return None
        key = self._key(message)
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            ts, value = item
            if now - ts > self.ttl_seconds:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value

    def put(self, message: str, value: TriageResult) -> None:
        if self.ttl_seconds <= 0:
            return
        key = self._key(message)
        with self._lock:
            self._items[key] = (time.monotonic(), value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
