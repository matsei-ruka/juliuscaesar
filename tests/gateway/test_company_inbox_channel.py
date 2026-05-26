"""Tests for the company-inbox gateway channel + its config wiring."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import config as gateway_config  # noqa: E402
from gateway.channels.company_inbox import CompanyInboxChannel  # noqa: E402
from gateway.config import ChannelConfig, ConfigError, load_config  # noqa: E402

from company.client import CompanyError  # noqa: E402


class FakeClient:
    """Stub CompanyClient. ``responses`` is consumed one per get_inbox call;
    an item may be an Exception to raise. Exhausted → empty inbox."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.alerts: list[dict] = []
        self.closed = 0

    def get_inbox(self, *, agent_id, statuses, limit):
        self.calls.append({"agent_id": agent_id, "statuses": statuses, "limit": limit})
        if not self.responses:
            return {"items": []}
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def post_alert(self, body):
        self.alerts.append(body)
        return {}

    def close(self):
        self.closed += 1


def _task(tid, **kw):
    base = {"id": tid, "title": f"Title {tid}", "description": "do the thing"}
    base.update(kw)
    return base


def _captured_enqueue():
    captured: list[dict] = []

    def enqueue(**kwargs):
        captured.append(kwargs)

    return captured, enqueue


def _make_channel(cfg=None, *, client=None, agent_id="agent-1", api_key="k1", log=None):
    ch = CompanyInboxChannel(
        Path("/tmp/jc-inbox-test"),
        cfg or ChannelConfig(enabled=True),
        log or (lambda *a, **k: None),
    )
    ch._company_cfg = SimpleNamespace(agent_id=agent_id, api_key=api_key)
    ch._client = client or FakeClient()
    return ch


class InjectionTests(unittest.TestCase):
    def test_inject_shape(self):
        captured, enqueue = _captured_enqueue()
        ch = _make_channel(
            client=FakeClient(
                [
                    {
                        "items": [
                            _task(
                                "t1",
                                root_id="r1",
                                parent_id="p1",
                                company_id="c1",
                                created_by={"kind": "user", "id": "u9"},
                                status="pending",
                                payload={"k": "v"},
                            )
                        ]
                    }
                ]
            )
        )
        self.assertEqual(ch._poll_once(enqueue), 1)
        kw = captured[0]
        self.assertEqual(kw["source"], "company-inbox")
        self.assertEqual(kw["source_message_id"], "task:t1")
        self.assertEqual(kw["conversation_id"], "task-root:r1")
        self.assertEqual(kw["user_id"], "u9")
        self.assertTrue(kw["content"].startswith("Title t1"))
        self.assertIn("do the thing", kw["content"])
        meta = kw["meta"]
        self.assertEqual(meta["kind"], "task_assigned")
        self.assertEqual(meta["task_id"], "t1")
        self.assertEqual(meta["root_id"], "r1")
        self.assertEqual(meta["company_status"], "pending")
        self.assertEqual(meta["payload"], {"k": "v"})

    def test_conversation_id_defaults_to_task_id_when_no_root(self):
        captured, enqueue = _captured_enqueue()
        ch = _make_channel(client=FakeClient([{"items": [_task("solo")]}]))
        ch._poll_once(enqueue)
        self.assertEqual(captured[0]["conversation_id"], "task-root:solo")

    def test_get_inbox_called_with_config(self):
        cfg = ChannelConfig(enabled=True, max_new_per_tick=4, inbox_status_filter=("pending",))
        client = FakeClient([{"items": []}])
        ch = _make_channel(cfg, client=client, agent_id="me")
        _, enqueue = _captured_enqueue()
        ch._poll_once(enqueue)
        call = client.calls[0]
        self.assertEqual(call["agent_id"], "me")
        self.assertEqual(call["statuses"], ("pending",))
        self.assertEqual(call["limit"], 8)  # max_new_per_tick * 2

    def test_dedup_seen_cache(self):
        captured, enqueue = _captured_enqueue()
        client = FakeClient([{"items": [_task("t1")]}, {"items": [_task("t1")]}])
        ch = _make_channel(client=client)
        ch._poll_once(enqueue)
        ch._poll_once(enqueue)
        self.assertEqual(len(captured), 1)

    def test_cap_defers_excess_to_next_tick(self):
        cfg = ChannelConfig(enabled=True, max_new_per_tick=2)
        tasks = [_task(f"t{i}", created_at=f"2026-01-0{i}") for i in range(1, 5)]
        client = FakeClient([{"items": list(tasks)}, {"items": list(tasks)}])
        captured, enqueue = _captured_enqueue()
        ch = _make_channel(cfg, client=client)
        self.assertEqual(ch._poll_once(enqueue), 2)
        self.assertEqual(
            [c["source_message_id"] for c in captured], ["task:t1", "task:t2"]
        )
        self.assertEqual(ch._poll_once(enqueue), 2)
        self.assertEqual(
            [c["source_message_id"] for c in captured][2:], ["task:t3", "task:t4"]
        )

    def test_orders_by_created_at_ascending(self):
        client = FakeClient(
            [
                {
                    "items": [
                        _task("b", created_at="2026-02-01"),
                        _task("a", created_at="2026-01-01"),
                    ]
                }
            ]
        )
        captured, enqueue = _captured_enqueue()
        ch = _make_channel(client=client)
        ch._poll_once(enqueue)
        self.assertEqual(
            [c["source_message_id"] for c in captured], ["task:a", "task:b"]
        )

    def test_ownership_mismatch_skipped(self):
        client = FakeClient([{"items": [_task("t1", owner_agent_id="other")]}])
        captured, enqueue = _captured_enqueue()
        ch = _make_channel(client=client, agent_id="me")
        self.assertEqual(ch._poll_once(enqueue), 0)
        self.assertEqual(captured, [])

    def test_accepts_tasks_key_and_bare_list(self):
        ch1 = _make_channel(client=FakeClient([{"tasks": [_task("t1")]}]))
        c1, e1 = _captured_enqueue()
        self.assertEqual(ch1._poll_once(e1), 1)

        ch2 = _make_channel(client=FakeClient([[_task("x")]]))
        c2, e2 = _captured_enqueue()
        self.assertEqual(ch2._poll_once(e2), 1)


class ErrorHandlingTests(unittest.TestCase):
    def test_transient_backoff_grows_then_recovers(self):
        ch = _make_channel(ChannelConfig(enabled=True, poll_interval_seconds=10))
        self.assertEqual(ch._handle_error(CompanyError("boom", status=502)), 20)
        self.assertEqual(ch._handle_error(CompanyError("boom", status=502)), 40)
        ch._on_success()
        self.assertEqual(ch.health()["consecutive_failures"], 0)
        self.assertTrue(ch.health()["auth_valid"])

    def test_backoff_capped_at_five_minutes(self):
        ch = _make_channel(ChannelConfig(enabled=True, poll_interval_seconds=120))
        interval = 0.0
        for _ in range(8):
            interval = ch._handle_error(CompanyError("x", status=500))
        self.assertLessEqual(interval, 300)

    def test_auth_failure_same_key_degrades_and_escalates_once(self):
        logs: list[tuple[str, dict]] = []
        ch = _make_channel(
            ChannelConfig(enabled=True, poll_interval_seconds=10),
            log=lambda m, **k: logs.append((m, k)),
            api_key="k1",
        )

        def fake_load():
            ch._company_cfg = SimpleNamespace(agent_id="me", api_key="k1")
            ch._client = FakeClient()

        ch._load_client = fake_load

        interval = ch._handle_error(CompanyError("401", status=401))
        self.assertEqual(interval, 40)  # poll_interval * DEGRADED_MULTIPLIER
        self.assertFalse(ch.health()["auth_valid"])
        self.assertTrue(ch.health()["degraded"])
        auth_logs = [m for m, k in logs if k.get("kind") == "company_inbox_auth_failure"]
        self.assertEqual(len(auth_logs), 1)

        # A second 401 stays degraded but does not re-escalate.
        ch._handle_error(CompanyError("401", status=401))
        auth_logs = [m for m, k in logs if k.get("kind") == "company_inbox_auth_failure"]
        self.assertEqual(len(auth_logs), 1)

    def test_auth_failure_new_key_retries_at_normal_interval(self):
        ch = _make_channel(
            ChannelConfig(enabled=True, poll_interval_seconds=10), api_key="old"
        )

        def fake_load():
            ch._company_cfg = SimpleNamespace(agent_id="me", api_key="new")
            ch._client = FakeClient()

        ch._load_client = fake_load
        interval = ch._handle_error(CompanyError("401", status=401))
        self.assertEqual(interval, 10)
        self.assertTrue(ch.health()["auth_valid"])

    def test_poll_error_never_propagates_from_run(self):
        # A get_inbox that always raises must not crash the run loop.
        ch = _make_channel(
            ChannelConfig(enabled=True, poll_interval_seconds=1),
            client=FakeClient([CompanyError("boom", status=503)]),
        )
        ch.ready = lambda: True
        ch._load_client = lambda: None  # keep the injected FakeClient
        stop = {"v": False}
        t = threading.Thread(
            target=ch.run, args=(lambda **k: None, lambda: stop["v"]), daemon=True
        )
        t.start()
        time.sleep(0.2)
        stop["v"] = True
        t.join(timeout=3)
        self.assertFalse(t.is_alive())


class RunLoopTests(unittest.TestCase):
    def test_run_injects_then_stops(self):
        client = FakeClient([{"items": [_task("t1")]}])
        ch = CompanyInboxChannel(
            Path("/tmp/jc-inbox-test"),
            ChannelConfig(enabled=True, poll_interval_seconds=1),
            lambda *a, **k: None,
        )
        ch.ready = lambda: True
        ch._company_cfg = SimpleNamespace(agent_id="me", api_key="k")
        ch._load_client = lambda: setattr(ch, "_client", client)

        captured, enqueue = _captured_enqueue()
        stop = {"v": False}
        t = threading.Thread(
            target=ch.run, args=(enqueue, lambda: stop["v"]), daemon=True
        )
        t.start()
        time.sleep(0.3)
        stop["v"] = True
        t.join(timeout=3)
        self.assertTrue(any(c["source_message_id"] == "task:t1" for c in captured))

    def test_ready_false_without_company_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "ops").mkdir()
            (instance / "memory" / "L1").mkdir(parents=True)
            gateway_config.clear_env_cache()
            ch = CompanyInboxChannel(instance, ChannelConfig(enabled=True), lambda *a, **k: None)
            self.assertFalse(ch.ready())


class ConfigWiringTests(unittest.TestCase):
    def _instance(self, tmp: str, yaml: str) -> Path:
        instance = Path(tmp)
        (instance / "ops").mkdir()
        (instance / "ops" / "gateway.yaml").write_text(yaml, encoding="utf-8")
        gateway_config.clear_config_cache()
        return instance

    def test_config_loads_company_inbox_knobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(
                tmp,
                "default_brain: claude\n"
                "channels:\n"
                "  company-inbox:\n"
                "    enabled: true\n"
                "    poll_interval_seconds: 30\n"
                "    max_new_per_tick: 3\n"
                "    inbox_status_filter: [pending]\n",
            )
            ch = load_config(instance).channel("company-inbox")
            self.assertTrue(ch.enabled)
            self.assertEqual(ch.poll_interval_seconds, 30)
            self.assertEqual(ch.max_new_per_tick, 3)
            self.assertEqual(ch.inbox_status_filter, ("pending",))

    def test_config_accepts_underscore_key_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(
                tmp,
                "default_brain: claude\nchannels:\n  company_inbox:\n    enabled: true\n",
            )
            self.assertTrue(load_config(instance).channel("company-inbox").enabled)

    def test_config_defaults_disabled_with_sane_knobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(tmp, "default_brain: claude\n")
            ch = load_config(instance).channel("company-inbox")
            self.assertFalse(ch.enabled)
            self.assertEqual(ch.poll_interval_seconds, 10)
            self.assertEqual(ch.max_new_per_tick, 5)
            self.assertEqual(ch.inbox_status_filter, ("pending", "accepted"))

    def test_config_rejects_bad_max_new_per_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(
                tmp,
                "default_brain: claude\nchannels:\n  company-inbox:\n"
                "    enabled: true\n    max_new_per_tick: -1\n",
            )
            with self.assertRaises(ConfigError):
                load_config(instance)

    def test_registered_in_factory(self):
        from gateway.channels.registry import _CHANNEL_FACTORIES

        self.assertIs(_CHANNEL_FACTORIES["company-inbox"], CompanyInboxChannel)


if __name__ == "__main__":
    unittest.main()
