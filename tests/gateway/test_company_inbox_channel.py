"""Tests for the company-inbox gateway channel + its config wiring."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import config as gateway_config  # noqa: E402
from gateway.channels import company_inbox as company_inbox_mod  # noqa: E402
from gateway.channels.company_inbox import CompanyInboxChannel  # noqa: E402
from gateway.config import ChannelConfig, ConfigError, load_config  # noqa: E402

from company.client import CompanyError  # noqa: E402


class FakeClient:
    """Stub CompanyClient. ``responses`` is consumed one per get_inbox call;
    an item may be an Exception to raise. Exhausted → empty inbox.

    ``whoami_responses`` (separate queue) is consumed one per whoami() call
    for the agent self-discovery boot path (PR #67 / spec
    ``docs/specs/agent-self-discovery.md``)."""

    def __init__(self, responses=None, *, whoami_responses=None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.whoami_calls = 0
        self.whoami_responses = list(whoami_responses or [])
        self.alerts: list[dict] = []
        self.comments: list[tuple[str, dict]] = []
        self.patches: list[tuple[str, dict]] = []
        self.completes: list[tuple[str, dict]] = []
        self.tasks: dict[str, dict] = {}
        self.update_responses: list[dict] = []
        self.update_calls: list[dict] = []
        self.closed = 0

    def get_inbox(self, *, agent_id, statuses, limit):
        self.calls.append({"agent_id": agent_id, "statuses": statuses, "limit": limit})
        if not self.responses:
            return {"items": []}
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def whoami(self):
        self.whoami_calls += 1
        if not self.whoami_responses:
            raise AssertionError("whoami() called without a queued response")
        result = self.whoami_responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def post_alert(self, body):
        self.alerts.append(body)
        return {}

    def comment_task(self, task_id, body):
        self.comments.append((task_id, body))
        return {"id": 99, "task_id": task_id, **body}

    def get_task(self, task_id):
        return self.tasks.get(task_id, {"id": task_id, "status": "pending"})

    def patch_task(self, task_id, body):
        self.patches.append((task_id, body))
        task = self.tasks.setdefault(task_id, {"id": task_id})
        task.update(body)
        return task

    def complete_task(self, task_id, body):
        self.completes.append((task_id, body))
        task = self.tasks.setdefault(task_id, {"id": task_id})
        task.update(
            {
                "status": body.get("status"),
                "result": body.get("result"),
                "approval_status": "pending" if body.get("approval_required") else "none",
            }
        )
        return task

    def list_task_updates(self, *, after_event_id=0, limit=50):
        self.update_calls.append({"after_event_id": after_event_id, "limit": limit})
        if not self.update_responses:
            return {"items": [], "next_cursor": after_event_id}
        return self.update_responses.pop(0)

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

    def test_send_writes_task_comment_and_accepts_pending_task(self):
        poll_client = FakeClient()
        send_client = FakeClient()
        ch = _make_channel(client=poll_client)
        ch.instance_dir = Path("/tmp/send-test")

        with (
            patch.object(company_inbox_mod.company_conf, "load", return_value=SimpleNamespace()),
            patch.object(company_inbox_mod, "CompanyClient", return_value=send_client),
        ):
            message_id = ch.send(
                "I saw it.",
                {
                    "task_id": "task-1",
                    "root_id": "root-1",
                    "conversation_id": "task-root:root-1",
                },
            )

        self.assertEqual(message_id, "task-comment:task-1:99")
        self.assertEqual(send_client.comments[0][0], "task-1")
        self.assertEqual(send_client.comments[0][1]["message"], "I saw it.")
        self.assertEqual(send_client.patches, [("task-1", {"status": "accepted"})])
        self.assertIs(ch._client, poll_client)
        self.assertEqual(poll_client.closed, 0)
        self.assertEqual(send_client.closed, 1)

    def test_send_comments_but_does_not_reaccept_non_pending_task(self):
        send_client = FakeClient()
        send_client.tasks["task-1"] = {"id": "task-1", "status": "in_progress"}
        ch = _make_channel()

        with (
            patch.object(company_inbox_mod.company_conf, "load", return_value=SimpleNamespace()),
            patch.object(company_inbox_mod, "CompanyClient", return_value=send_client),
        ):
            message_id = ch.send("Still working.", {"task_id": "task-1"})

        self.assertEqual(message_id, "task-comment:task-1:99")
        self.assertEqual(len(send_client.comments), 1)
        self.assertEqual(send_client.patches, [])

    def test_send_final_envelope_closes_task_with_result(self):
        send_client = FakeClient()
        ch = _make_channel()
        response = (
            '{"company_task":{"status":"done","comment":"Published.",'
            '"result":{"url":"https://example.test/post","title":"Post",'
            '"summary":"One line."}}}'
        )

        with (
            patch.object(company_inbox_mod.company_conf, "load", return_value=SimpleNamespace()),
            patch.object(company_inbox_mod, "CompanyClient", return_value=send_client),
        ):
            message_id = ch.send(response, {"task_id": "task-1", "root_id": "root-1"})

        self.assertEqual(message_id, "task-complete:task-1")
        self.assertEqual(send_client.comments, [])
        self.assertEqual(send_client.patches, [])
        self.assertEqual(
            send_client.completes,
            [
                (
                    "task-1",
                    {
                        "status": "done",
                        "comment": "Published.",
                        "result": {
                            "url": "https://example.test/post",
                            "title": "Post",
                            "summary": "One line.",
                        },
                        "approval_required": False,
                    },
                ),
            ],
        )

    def test_send_final_envelope_with_footer_still_closes_task(self):
        send_client = FakeClient()
        ch = _make_channel()

        with (
            patch.object(company_inbox_mod.company_conf, "load", return_value=SimpleNamespace()),
            patch.object(company_inbox_mod, "CompanyClient", return_value=send_client),
        ):
            ch.send(
                '{"company_task":{"status":"done","comment":"Closed.",'
                '"result":{"payload":{"summary":"ok"}}}}\n\nfooter text',
                {"task_id": "task-1"},
            )

        self.assertEqual(send_client.comments, [])
        self.assertEqual(send_client.patches, [])
        self.assertEqual(send_client.completes[-1][1]["status"], "done")
        self.assertEqual(send_client.completes[-1][1]["comment"], "Closed.")
        self.assertEqual(
            send_client.completes[-1][1]["result"],
            {"payload": {"summary": "ok"}},
        )

    def test_send_final_envelope_passes_approval_required_to_complete(self):
        send_client = FakeClient()
        ch = _make_channel()

        with (
            patch.object(company_inbox_mod.company_conf, "load", return_value=SimpleNamespace()),
            patch.object(company_inbox_mod, "CompanyClient", return_value=send_client),
        ):
            ch.send(
                '{"company_task":{"status":"done","comment":"Draft ready.",'
                '"result":{"draft":"Post body","approval_required":true}}}',
                {"task_id": "task-1"},
            )

        self.assertEqual(send_client.completes[-1][1]["approval_required"], True)
        self.assertEqual(send_client.completes[-1][1]["result"], {"draft": "Post body"})

    def test_send_task_update_notification_does_not_write_back(self):
        send_client = FakeClient()
        ch = _make_channel()

        with (
            patch.object(company_inbox_mod.company_conf, "load", return_value=SimpleNamespace()),
            patch.object(company_inbox_mod, "CompanyClient", return_value=send_client),
        ):
            message_id = ch.send(
                "Noted.",
                {"kind": "task_updated", "task_id": "task-1", "event_id": "42"},
            )

        self.assertEqual(message_id, "task-update-ack:42")
        self.assertEqual(send_client.comments, [])
        self.assertEqual(send_client.patches, [])
        self.assertEqual(send_client.completes, [])

    def test_poll_updates_injects_participant_notification(self):
        captured, enqueue = _captured_enqueue()
        client = FakeClient([{"items": []}])
        client.update_responses = [
            {
                "items": [
                    {
                        "event": {
                            "id": 11,
                            "event_type": "comment_added",
                            "payload": {"message": "Done with URL."},
                        },
                        "task": {
                            "id": "task-1",
                            "root_id": "root-1",
                            "title": "Daily blog",
                            "status": "done",
                            "result": {"url": "https://example.test/post"},
                        },
                    }
                ],
                "next_cursor": 11,
            }
        ]
        ch = _make_channel(client=client)
        ch._updates_cursor = 10

        self.assertEqual(ch._poll_once(enqueue), 1)
        self.assertEqual(captured[0]["source_message_id"], "task-update:11")
        self.assertEqual(captured[0]["conversation_id"], "task-root:root-1")
        self.assertEqual(captured[0]["meta"]["kind"], "task_updated")
        self.assertIn("Done with URL.", captured[0]["content"])
        self.assertIn("Notification only", captured[0]["content"])
        self.assertEqual(ch._updates_cursor, 11)

    def test_poll_updates_coalesces_multiple_events_for_same_task(self):
        captured, enqueue = _captured_enqueue()
        client = FakeClient([{"items": []}])
        client.update_responses = [
            {
                "items": [
                    {
                        "event": {"id": 11, "event_type": "comment_added", "payload": {}},
                        "task": {"id": "task-1", "root_id": "root-1", "title": "Task", "status": "accepted"},
                    },
                    {
                        "event": {"id": 12, "event_type": "status_changed", "payload": {}},
                        "task": {"id": "task-1", "root_id": "root-1", "title": "Task", "status": "done"},
                    },
                ],
                "next_cursor": 12,
            }
        ]
        ch = _make_channel(client=client)
        ch._updates_cursor = 10

        self.assertEqual(ch._poll_once(enqueue), 1)
        self.assertEqual(captured[0]["source_message_id"], "task-update:12")
        self.assertIn("Status: done", captured[0]["content"])
        self.assertEqual(ch._updates_cursor, 12)


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

    def test_config_loads_emit_task_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(
                tmp,
                "default_brain: claude\nchannels:\n  company-inbox:\n"
                "    enabled: true\n    emit_task_closed: true\n",
            )
            self.assertTrue(load_config(instance).channel("company-inbox").emit_task_closed)

    def test_config_rejects_bad_emit_task_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(
                tmp,
                "default_brain: claude\nchannels:\n  company-inbox:\n"
                "    enabled: true\n    emit_task_closed: maybe\n",
            )
            with self.assertRaises(ConfigError):
                load_config(instance)


class MetaTests(unittest.TestCase):
    def test_inject_meta_carries_title_and_description(self):
        captured, enqueue = _captured_enqueue()
        ch = _make_channel(
            client=FakeClient([{"items": [_task("t1", title="Onboard X", description="Run it")]}])
        )
        ch._poll_once(enqueue)
        meta = captured[0]["meta"]
        self.assertEqual(meta["title"], "Onboard X")
        self.assertEqual(meta["description"], "Run it")


class ClosureTests(unittest.TestCase):
    def _ch(self, client):
        return _make_channel(
            ChannelConfig(enabled=True, emit_task_closed=True), client=client
        )

    def test_disappeared_injected_task_emits_task_closed(self):
        client = FakeClient([{"items": [_task("t1")]}, {"items": []}])
        captured, enqueue = _captured_enqueue()
        ch = self._ch(client)
        ch._poll_once(enqueue)  # inject t1
        ch._poll_once(enqueue)  # t1 gone → close
        kinds = [(c["source_message_id"], c["meta"]["kind"]) for c in captured]
        self.assertEqual(kinds, [("task:t1", "task_assigned"), ("task-closed:t1", "task_closed")])
        # t1 dropped from tracking + seen so it can't double-close
        self.assertNotIn("t1", ch._injected)

    def test_in_progress_task_not_closed(self):
        # pending → in_progress (still present in wide poll) must NOT close.
        client = FakeClient(
            [
                {"items": [_task("t1", status="pending")]},
                {"items": [_task("t1", status="in_progress")]},
                {"items": []},
            ]
        )
        captured, enqueue = _captured_enqueue()
        ch = self._ch(client)
        ch._poll_once(enqueue)  # inject (pending)
        ch._poll_once(enqueue)  # in_progress, still present → no close
        closes_after_2 = [c for c in captured if c["meta"]["kind"] == "task_closed"]
        self.assertEqual(closes_after_2, [])
        ch._poll_once(enqueue)  # now absent → close
        closes = [c["source_message_id"] for c in captured if c["meta"]["kind"] == "task_closed"]
        self.assertEqual(closes, ["task-closed:t1"])

    def test_in_progress_task_not_injected(self):
        # A task seen only in in_progress is tracked-for-presence but not injected.
        client = FakeClient([{"items": [_task("t1", status="in_progress")]}])
        captured, enqueue = _captured_enqueue()
        ch = self._ch(client)
        ch._poll_once(enqueue)
        self.assertEqual(captured, [])

    def test_truncated_poll_suppresses_closure(self):
        from gateway.channels.company_inbox import CLOSURE_POLL_LIMIT

        big = [_task(f"t{i}") for i in range(CLOSURE_POLL_LIMIT)]
        client = FakeClient([{"items": [_task("t1")]}, {"items": big}])
        captured, enqueue = _captured_enqueue()
        ch = self._ch(client)
        ch._poll_once(enqueue)  # inject t1
        ch._poll_once(enqueue)  # full page (truncated) and t1 absent → NO close
        closes = [c for c in captured if c["meta"]["kind"] == "task_closed"]
        self.assertEqual(closes, [])

    def test_disabled_by_default_no_closure(self):
        client = FakeClient([{"items": [_task("t1")]}, {"items": []}])
        captured, enqueue = _captured_enqueue()
        ch = _make_channel(client=client)  # emit_task_closed defaults False
        ch._poll_once(enqueue)
        ch._poll_once(enqueue)
        closes = [c for c in captured if c["meta"]["kind"] == "task_closed"]
        self.assertEqual(closes, [])


class DiscoveryTests(unittest.TestCase):
    """Boot-time COMPANY_AGENT_ID discovery via /api/agents/me.

    Spec: docs/specs/agent-self-discovery.md §4 + §7.

    The four required cases:
      (a) fresh .env without COMPANY_AGENT_ID → one /me call, persists,
          polling resumes with the discovered id;
      (b) .env already has COMPANY_AGENT_ID → no /me call ever;
      (c) /me returns 401 → channel goes degraded, no /inbox call;
      (d) network error on /me → degraded retry loop, no /inbox call.

    These wire `run()` end-to-end through a controlled instance dir so
    we can also verify the .env write is atomic + mode-preserving (§4.1.a).
    """

    AGENT_UUID = "6483bd0a-f750-4388-b9f5-52b02d0491ad"

    def _write_env(self, instance: Path, lines: dict[str, str]) -> Path:
        env_path = instance / ".env"
        env_path.write_text(
            "\n".join(f"{k}={v}" for k, v in lines.items()) + "\n",
            encoding="utf-8",
        )
        env_path.chmod(0o600)
        return env_path

    def _run_until(self, ch, enqueue, *, predicate, timeout=3.0):
        """Spin `run()` in a thread until predicate() goes True or timeout."""
        stop = {"v": False}

        def _should_stop():
            return stop["v"]

        t = threading.Thread(
            target=ch.run, args=(enqueue, _should_stop), daemon=True
        )
        t.start()
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if predicate():
                    break
                time.sleep(0.02)
        finally:
            stop["v"] = True
            t.join(timeout=2.0)
        return not t.is_alive()

    def _patch_load_client(self, ch, instance: Path, client):
        """Make `_load_client` re-read .env into a SimpleNamespace cfg.

        We bypass `company.conf.load()` because the gateway env_value
        cache can be sticky across temp dirs; reading the .env directly
        gives us a deterministic, self-contained fixture for these tests.
        The production code path is exercised by the existing reporter
        tests.
        """
        def _load():
            env_path = instance / ".env"
            kv: dict[str, str] = {}
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip().strip('"')
            ch._company_cfg = SimpleNamespace(
                endpoint=kv.get("COMPANY_ENDPOINT", "https://t.local"),
                api_key=kv.get("COMPANY_API_KEY", ""),
                enrollment_token=kv.get("COMPANY_ENROLLMENT_TOKEN", ""),
                agent_id=kv.get("COMPANY_AGENT_ID", ""),
            )
            ch._client = client

        ch._load_client = _load

    # ── §7(a) ───────────────────────────────────────────────────────────
    def test_fresh_env_discovers_persists_then_polls(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            env_path = self._write_env(
                instance,
                {
                    "COMPANY_ENDPOINT": "https://t.local",
                    "COMPANY_API_KEY": "k1",
                },
            )
            client = FakeClient(
                responses=[{"items": [_task("t1")]}],
                whoami_responses=[
                    {
                        "id": self.AGENT_UUID,
                        "slug": "noah_bitwell",
                        "display_name": "Noah Bitwell",
                        "company": {"id": "c", "slug": "omnisage"},
                    }
                ],
            )
            logs: list[tuple[str, dict]] = []
            ch = CompanyInboxChannel(
                instance,
                ChannelConfig(enabled=True, poll_interval_seconds=1),
                lambda m, **k: logs.append((m, k)),
            )
            ch.ready = lambda: True
            self._patch_load_client(ch, instance, client)

            captured, enqueue = _captured_enqueue()
            ok = self._run_until(
                ch, enqueue, predicate=lambda: len(captured) >= 1
            )

            self.assertTrue(ok, "run() thread did not stop")
            # Exactly one /me call.
            self.assertEqual(client.whoami_calls, 1)
            # .env now contains the discovered id, mode preserved at 0600.
            content = env_path.read_text(encoding="utf-8")
            self.assertIn(f"COMPANY_AGENT_ID={self.AGENT_UUID}", content)
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            # Polling resumed and a task was injected with the discovered id.
            self.assertGreaterEqual(len(client.calls), 1)
            self.assertEqual(client.calls[0]["agent_id"], self.AGENT_UUID)
            self.assertEqual(captured[0]["source_message_id"], "task:t1")
            # health() reflects discovery success.
            self.assertTrue(ch.health()["agent_id_discovered"])
            self.assertFalse(ch.health()["degraded"])

    # ── §7(b) ───────────────────────────────────────────────────────────
    def test_env_with_agent_id_skips_discovery_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._write_env(
                instance,
                {
                    "COMPANY_ENDPOINT": "https://t.local",
                    "COMPANY_API_KEY": "k1",
                    "COMPANY_AGENT_ID": self.AGENT_UUID,
                },
            )
            # No whoami_responses → if /me is called the FakeClient raises
            # AssertionError and the test fails loudly.
            client = FakeClient(responses=[{"items": [_task("t1")]}])
            ch = CompanyInboxChannel(
                instance,
                ChannelConfig(enabled=True, poll_interval_seconds=1),
                lambda *a, **k: None,
            )
            ch.ready = lambda: True
            self._patch_load_client(ch, instance, client)

            captured, enqueue = _captured_enqueue()
            ok = self._run_until(
                ch, enqueue, predicate=lambda: len(captured) >= 1
            )
            self.assertTrue(ok)
            self.assertEqual(client.whoami_calls, 0)
            # Inbox polled with the pre-existing id.
            self.assertEqual(client.calls[0]["agent_id"], self.AGENT_UUID)
            self.assertTrue(ch.health()["agent_id_discovered"] is False)
            # Note: agent_id_discovered stays False in this path because the
            # channel did not run discovery — it trusted the .env. The
            # operator-facing surface that matters is `auth_valid` +
            # `last_injected_at`, which both reflect a healthy channel.

    # ── §7(c) ───────────────────────────────────────────────────────────
    def test_me_401_goes_degraded_no_inbox_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._write_env(
                instance,
                {
                    "COMPANY_ENDPOINT": "https://t.local",
                    "COMPANY_API_KEY": "revoked",
                },
            )
            # Always 401 — the same key on every reload, so no recovery.
            client = FakeClient(
                whoami_responses=[
                    CompanyError("401", status=401),
                    CompanyError("401", status=401),
                    CompanyError("401", status=401),
                    CompanyError("401", status=401),
                ]
            )
            logs: list[tuple[str, dict]] = []
            ch = CompanyInboxChannel(
                instance,
                ChannelConfig(enabled=True, poll_interval_seconds=1),
                lambda m, **k: logs.append((m, k)),
            )
            ch.ready = lambda: True
            self._patch_load_client(ch, instance, client)

            captured, enqueue = _captured_enqueue()
            ok = self._run_until(
                ch,
                enqueue,
                predicate=lambda: any(
                    k.get("kind") == "company_inbox_discovery_failure"
                    for _, k in logs
                ),
                timeout=10.0,
            )
            self.assertTrue(ok, "discovery failure WARN never emitted")
            # /inbox MUST never be called — that is the silent-400 bug we
            # exist to kill. `client.calls` only records get_inbox calls.
            self.assertEqual(client.calls, [])
            # Degraded surface reflects the failure.
            h = ch.health()
            self.assertFalse(h["agent_id_discovered"])
            self.assertTrue(h["degraded"])
            self.assertIn("discovery:", h["last_error"] or "")

    # ── §7(d) ───────────────────────────────────────────────────────────
    def test_me_network_error_degraded_retry_loop_no_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._write_env(
                instance,
                {
                    "COMPANY_ENDPOINT": "https://t.local",
                    "COMPANY_API_KEY": "k1",
                },
            )
            # Transport failure (status=0). On the spec's degraded cadence
            # (poll_interval * DEGRADED_MULTIPLIER = 4s) we won't get
            # to attempt 3 before the test timeout — so we time out on
            # `consecutive_failures >= 1` and verify the surface state.
            client = FakeClient(
                whoami_responses=[
                    CompanyError("transport: connection refused", status=0),
                    CompanyError("transport: connection refused", status=0),
                    CompanyError("transport: connection refused", status=0),
                ]
            )
            ch = CompanyInboxChannel(
                instance,
                ChannelConfig(enabled=True, poll_interval_seconds=1),
                lambda *a, **k: None,
            )
            ch.ready = lambda: True
            self._patch_load_client(ch, instance, client)

            captured, enqueue = _captured_enqueue()
            ok = self._run_until(
                ch,
                enqueue,
                predicate=lambda: ch.health()["consecutive_failures"] >= 1,
                timeout=5.0,
            )
            self.assertTrue(ok)
            # Inbox MUST never have been called — discovery never resolved.
            self.assertEqual(client.calls, [])
            self.assertGreaterEqual(client.whoami_calls, 1)
            h = ch.health()
            self.assertFalse(h["agent_id_discovered"])
            self.assertTrue(h["degraded"])


class AgentIdAccessorTests(unittest.TestCase):
    """Spec §4.3: `_agent_id()` no longer falls back to instance_id."""

    def test_agent_id_returns_empty_when_cfg_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            ch = CompanyInboxChannel(
                instance, ChannelConfig(enabled=True), lambda *a, **k: None
            )
            ch._company_cfg = SimpleNamespace(agent_id="", api_key="k")
            # Spec §4.3: the SHA256 fallback is gone. The accessor returns
            # the configured value verbatim, including the empty string.
            # The run() entrypoint refuses to poll until discovery has
            # populated this — see DiscoveryTests above.
            self.assertEqual(ch._agent_id(), "")

    def test_agent_id_returns_configured_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            ch = CompanyInboxChannel(
                instance, ChannelConfig(enabled=True), lambda *a, **k: None
            )
            ch._company_cfg = SimpleNamespace(agent_id="known-id", api_key="k")
            self.assertEqual(ch._agent_id(), "known-id")


if __name__ == "__main__":
    unittest.main()
