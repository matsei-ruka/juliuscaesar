"""Persistent store for brain-level failures (auth expiry, credit exhaustion).

Maps brain adapter names (e.g. 'claude', 'pi') to Unix failure timestamps.
Written atomically to `state/gateway/brain_failure.json`.

Lifecycle: survives gateway restarts.  Clear by calling `.clear(brain)` or
deleting the file.  The in-memory set is authoritative once loaded; file is
read only at startup.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


_STORE_FILENAME = "brain_failure.json"


# Failure marks expire after this long by default. The store used to be
# permanent with no .clear() call site anywhere — one auth blip diverted
# routing forever, surviving restarts (audit D-P2 / feature 5).
DEFAULT_FAILURE_TTL_SECONDS = 6 * 3600.0


class BrainFailureStore:
    def __init__(
        self,
        instance_dir: Path,
        *,
        ttl_seconds: float = DEFAULT_FAILURE_TTL_SECONDS,
    ) -> None:
        self._path = instance_dir / "state" / "gateway" / _STORE_FILENAME
        self._ttl_seconds = float(ttl_seconds)
        self._failed: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._failed = {
                    str(k): float(v)
                    for k, v in data.items()
                    if isinstance(v, (int, float))
                }
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._failed = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._failed, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    def mark_failed(self, brain: str) -> None:
        """Record `brain` as failed at now. Idempotent."""
        self._failed[brain] = time.time()
        self._save()

    def is_failed(self, brain: str) -> bool:
        marked_at = self._failed.get(brain)
        if marked_at is None:
            return False
        if self._ttl_seconds > 0 and time.time() - marked_at > self._ttl_seconds:
            # Expired — give the brain another chance and persist the drop.
            del self._failed[brain]
            self._save()
            return False
        return True

    def clear(self, brain: str) -> None:
        """Remove brain from the failed set and persist."""
        if brain in self._failed:
            del self._failed[brain]
            self._save()

    def all_failed(self) -> list[str]:
        return list(self._failed)
