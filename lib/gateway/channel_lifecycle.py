"""Gateway channel lifecycle management.

Audit feature 4 (B-P1): channels used to run on bare daemon threads — one
constructor exception killed the gateway at boot, and a channel thread dying
mid-run was silent forever (the gateway heartbeat stays fresh, the watchdog
sees healthy, Telegram inbound is dark). Channels are now supervised:

- constructor failures are isolated per channel (the others still boot);
- a crashed channel thread is rebuilt + restarted with exponential backoff
  (5s → cap 300s, reset after a stable ≥300s run);
- a clean ``run()`` return is restart-worthy too when the channel had been
  running ≥60s (long-pollers don't return mid-flight) — but a first run
  returning quickly is treated as a deliberate no-op (e.g. telegram with no
  token) and parked as ``not-ready`` instead of restart-looping;
- per-channel health is exposed via ``health_snapshot()`` and persisted to
  ``state/gateway/channel_health.json`` for ``jc doctor --json``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .channels import enabled_channel_factories
from .channels.base import Channel, LogFn
from .config import GatewayConfig

_BACKOFF_INITIAL_SECONDS = 5.0
_BACKOFF_MAX_SECONDS = 300.0
# A run shorter than this on the FIRST attempt = deliberate no-op channel.
_QUICK_EXIT_SECONDS = 60.0
# A run at least this long resets the backoff to the initial value.
_STABLE_RUN_SECONDS = 300.0


class ChannelLifecycle:
    """Owns live channel instances and their supervised runner threads."""

    def __init__(
        self,
        instance_dir: Path,
        *,
        config: GatewayConfig,
        log: LogFn,
        enqueue: Callable[..., None],
        stop_requested: Callable[[], bool],
    ):
        self.instance_dir = instance_dir
        self.config = config
        self.log = log
        self.enqueue = enqueue
        self.stop_requested = stop_requested
        self.channels: dict[str, Channel] = {}
        self.threads: list[threading.Thread] = []
        self._health: dict[str, dict[str, Any]] = {}
        self._health_lock = threading.Lock()

    def reload_config(self, config: GatewayConfig) -> None:
        self.config = config

    # ------------------------------------------------------------- health

    def _set_health(self, name: str, **fields: Any) -> None:
        with self._health_lock:
            entry = self._health.setdefault(
                name, {"state": "unknown", "restarts": 0, "last_error": None}
            )
            entry.update(fields)
            snapshot = {k: dict(v) for k, v in self._health.items()}
        self._write_health_file(snapshot)

    def health_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._health_lock:
            return {k: dict(v) for k, v in self._health.items()}

    def _write_health_file(self, snapshot: dict[str, dict[str, Any]]) -> None:
        path = self.instance_dir / "state" / "gateway" / "channel_health.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # observability only — never disturb channel work

    # -------------------------------------------------------------- start

    def start(self) -> None:
        factories = enabled_channel_factories(self.instance_dir, self.config, self.log)
        for name, factory in factories.items():
            try:
                channel = factory()
            except Exception as exc:  # noqa: BLE001 — constructor isolation
                self.log(f"channel build failed name={name}: {exc!r} — skipped")
                self._set_health(name, state="build-failed", last_error=repr(exc))
                continue
            self.channels[channel.name] = channel
            self._set_health(channel.name, state="running", last_error=None)
            thread = threading.Thread(
                target=self._supervise,
                args=(channel.name, factory),
                name=f"gateway-{channel.name}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

    # ---------------------------------------------------------- supervise

    def _supervise(self, name: str, factory: Callable[[], Channel]) -> None:
        backoff = _BACKOFF_INITIAL_SECONDS
        first_run = True
        while not self.stop_requested():
            channel = self.channels.get(name)
            if channel is None:
                return
            started = time.monotonic()
            error: BaseException | None = None
            try:
                channel.run(self.enqueue, self.stop_requested)
            except Exception as exc:  # noqa: BLE001 — supervised restart
                error = exc
            ran_for = time.monotonic() - started
            if self.stop_requested():
                self._set_health(name, state="stopped")
                return
            if error is None and first_run and ran_for < _QUICK_EXIT_SECONDS:
                # Deliberate no-op (disabled/not-ready channel). Restarting
                # it would just spin; park it and stay quiet.
                self.log(
                    f"channel exited cleanly after {ran_for:.0f}s name={name} — "
                    "treating as not-ready, supervision parked"
                )
                self._set_health(name, state="not-ready")
                return
            first_run = False
            if ran_for >= _STABLE_RUN_SECONDS:
                backoff = _BACKOFF_INITIAL_SECONDS
            reason = f"crashed: {error!r}" if error is not None else "returned mid-run"
            self.log(
                f"channel {reason} name={name} after {ran_for:.0f}s — "
                f"restarting in {backoff:.0f}s",
            )
            self._set_health(
                name,
                state="backoff",
                last_error=repr(error) if error is not None else "returned mid-run",
                last_exit_at=time.time(),
            )
            with self._health_lock:
                self._health[name]["restarts"] = (
                    int(self._health[name].get("restarts", 0)) + 1
                )
            if not self._interruptible_sleep(backoff):
                self._set_health(name, state="stopped")
                return
            backoff = min(_BACKOFF_MAX_SECONDS, backoff * 2)
            # Rebuild the instance — a crashed poller may hold broken
            # sockets or a wedged sqlite handle.
            try:
                self.channels[name] = factory()
            except Exception as exc:  # noqa: BLE001
                self.log(f"channel rebuild failed name={name}: {exc!r} — will retry")
                self._set_health(name, state="build-failed", last_error=repr(exc))
                continue
            self._set_health(name, state="running")

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep in 1s slices; False when stop was requested mid-sleep."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stop_requested():
                return False
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        return not self.stop_requested()

    # -------------------------------------------------------------- close

    def close(self) -> None:
        for thread in self.threads:
            thread.join(timeout=2)
        self.threads.clear()
        for channel in list(self.channels.values()):
            close = getattr(channel, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # noqa: BLE001
                    self.log(f"channel close failed channel={channel.name}: {exc}")
        self.channels.clear()
