"""Audit feature 4 — per-channel fault isolation + supervised threads."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

import gateway.channel_lifecycle as lifecycle_mod  # noqa: E402
from gateway.channel_lifecycle import ChannelLifecycle  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402


class _FakeChannel:
    def __init__(self, name: str, behavior):
        self.name = name
        self._behavior = behavior
        self.runs = 0

    def ready(self) -> bool:
        return True

    def run(self, enqueue, should_stop):
        self.runs += 1
        self._behavior(self, should_stop)

    def send(self, response, meta):
        return None


def _lifecycle(instance: Path, factories: dict, stop_flag: threading.Event):
    lc = ChannelLifecycle(
        instance,
        config=GatewayConfig(),
        log=lambda *a, **k: None,
        enqueue=lambda **k: None,
        stop_requested=stop_flag.is_set,
    )
    # Bypass config-driven discovery: inject fakes directly via the same
    # start path the real factories use.
    lifecycle_mod_factories = factories

    def fake_enabled_channel_factories(instance_dir, config, log):
        return lifecycle_mod_factories

    return lc, fake_enabled_channel_factories


class SupervisionTests(unittest.TestCase):
    def _start(self, factories):
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            lc, fake_factories = _lifecycle(instance, factories, stop)
            orig = lifecycle_mod.enabled_channel_factories
            lifecycle_mod.enabled_channel_factories = fake_factories
            try:
                lc.start()
                yield_time = time.time() + 3.0
                yield lc, stop, instance
            finally:
                stop.set()
                lifecycle_mod.enabled_channel_factories = orig
                lc.close()

    def test_boot_survives_raising_constructor(self):
        built = []

        def bad_factory():
            raise RuntimeError("boom at build")

        def good_factory():
            ch = _FakeChannel("good", lambda self, stop: stop() or time.sleep(0.05))
            built.append(ch)
            return ch

        gen = self._start({"bad": bad_factory, "good": good_factory})
        lc, stop, instance = next(gen)
        try:
            time.sleep(0.2)
            health = lc.health_snapshot()
            self.assertEqual(health["bad"]["state"], "build-failed")
            self.assertTrue(built)  # good channel still built + started
        finally:
            try:
                next(gen)
            except StopIteration:
                pass

    def test_crashed_channel_restarts_with_rebuilt_instance(self):
        instances = []
        crashes = threading.Event()

        def crashing(self, should_stop):
            if len(instances) == 1:
                raise RuntimeError("poller died")
            crashes.set()
            while not should_stop():
                time.sleep(0.02)

        def factory():
            ch = _FakeChannel("crashy", crashing)
            instances.append(ch)
            return ch

        # Shrink backoff so the test is fast.
        old_initial = lifecycle_mod._BACKOFF_INITIAL_SECONDS
        lifecycle_mod._BACKOFF_INITIAL_SECONDS = 0.05
        try:
            gen = self._start({"crashy": factory})
            lc, stop, instance = next(gen)
            try:
                self.assertTrue(crashes.wait(timeout=5.0))
                self.assertGreaterEqual(len(instances), 2)  # rebuilt, not reused
                health = lc.health_snapshot()
                self.assertGreaterEqual(health["crashy"]["restarts"], 1)
                # health file persisted
                payload = json.loads(
                    (instance / "state" / "gateway" / "channel_health.json").read_text()
                )
                self.assertIn("crashy", payload)
            finally:
                try:
                    next(gen)
                except StopIteration:
                    pass
        finally:
            lifecycle_mod._BACKOFF_INITIAL_SECONDS = old_initial

    def test_quick_clean_first_return_parks_as_not_ready(self):
        def noop(self, should_stop):
            return  # e.g. telegram with no token

        def factory():
            return _FakeChannel("quiet", noop)

        gen = self._start({"quiet": factory})
        lc, stop, instance = next(gen)
        try:
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if lc.health_snapshot().get("quiet", {}).get("state") == "not-ready":
                    break
                time.sleep(0.05)
            health = lc.health_snapshot()
            self.assertEqual(health["quiet"]["state"], "not-ready")
            self.assertEqual(health["quiet"]["restarts"], 0)
        finally:
            try:
                next(gen)
            except StopIteration:
                pass


class RegistryIsolationTests(unittest.TestCase):
    def test_build_enabled_channels_skips_raising_factory(self):
        from gateway.channels import registry

        calls = []

        def fake_factories(instance_dir, config, log):
            return {
                "boom": lambda: (_ for _ in ()).throw(RuntimeError("nope")),
                "fine": lambda: _FakeChannel("fine", lambda s, st: None),
            }

        orig = registry.enabled_channel_factories
        registry.enabled_channel_factories = fake_factories
        try:
            chans = registry.build_enabled_channels(
                Path("/tmp"), GatewayConfig(), lambda m: calls.append(m)
            )
        finally:
            registry.enabled_channel_factories = orig
        self.assertEqual([c.name for c in chans], ["fine"])
        self.assertTrue(any("boom" in m for m in calls))


if __name__ == "__main__":
    unittest.main()
