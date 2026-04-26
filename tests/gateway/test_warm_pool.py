"""Unit tests for the warm pool (lib/gateway/warm_pool/).

We mock out PoolProcess so tests don't shell out to a real claude binary.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.warm_pool import (  # noqa: E402
    PoolManager,
    PoolProcessError,
    encode_user_message,
    parse_event_line,
)
from gateway.warm_pool.protocol import (  # noqa: E402
    InvokeResult,
    extract_result,
    is_terminal_event,
)


class ProtocolTests(unittest.TestCase):
    def test_encode_user_message_roundtrip(self) -> None:
        line = encode_user_message("hello world")
        data = json.loads(line)
        self.assertEqual(data["type"], "user")
        self.assertEqual(data["message"]["role"], "user")
        self.assertEqual(data["message"]["content"], "hello world")
        # No newlines in payload — caller adds the framing newline.
        self.assertNotIn("\n", line)

    def test_encode_unicode(self) -> None:
        line = encode_user_message("ciao 💄")
        self.assertIn("💄", line)
        self.assertEqual(json.loads(line)["message"]["content"], "ciao 💄")

    def test_parse_event_line_valid(self) -> None:
        evt = parse_event_line('{"type":"result","session_id":"abc"}')
        self.assertEqual(evt, {"type": "result", "session_id": "abc"})

    def test_parse_event_line_garbage(self) -> None:
        self.assertIsNone(parse_event_line(""))
        self.assertIsNone(parse_event_line("   "))
        self.assertIsNone(parse_event_line("not-json"))
        self.assertIsNone(parse_event_line("[1,2,3]"))  # not a dict

    def test_is_terminal_event(self) -> None:
        self.assertTrue(is_terminal_event({"type": "result"}))
        self.assertFalse(is_terminal_event({"type": "assistant"}))
        self.assertFalse(is_terminal_event({"type": "system", "subtype": "init"}))

    def test_extract_result_happy_path(self) -> None:
        events = [
            {"type": "system", "subtype": "init", "session_id": "s1"},
            {
                "type": "assistant",
                "session_id": "s1",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "..."},
                        {"type": "text", "text": "ONE"},
                    ]
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ONE",
                "stop_reason": "end_turn",
                "session_id": "s1",
            },
        ]
        result = extract_result(events)
        self.assertEqual(result.text, "ONE")
        self.assertEqual(result.session_id, "s1")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertFalse(result.is_error)

    def test_extract_result_error(self) -> None:
        events = [
            {
                "type": "result",
                "is_error": True,
                "result": "Not logged in",
                "session_id": "s2",
            },
        ]
        result = extract_result(events)
        self.assertTrue(result.is_error)
        self.assertEqual(result.session_id, "s2")
        self.assertEqual(result.error_text, "Not logged in")

    def test_extract_result_falls_back_to_assistant_text(self) -> None:
        events = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "partial"}]},
            },
            {"type": "result", "is_error": False, "session_id": "s3"},
        ]
        result = extract_result(events)
        # No `result.result` field — extractor falls back to assistant text.
        self.assertEqual(result.text, "partial")

    def test_extract_result_no_terminal(self) -> None:
        events = [{"type": "assistant", "message": {"content": []}}]
        result = extract_result(events)
        self.assertTrue(result.is_error)
        self.assertEqual(result.error_text, "no result event in stream")


class FakePoolProcess:
    """Mimics the PoolProcess surface the manager uses."""

    spawn_count = 0

    def __init__(self, *, key: tuple[str, str, str | None], should_fail: bool = False):
        type(self).spawn_count += 1
        self.key = key
        self.should_fail = should_fail
        self.session_id: str | None = None
        self.last_used = time.monotonic()
        self.message_count = 0
        self.healthy = False
        self._alive = False
        self.terminated = False

    def start(self) -> None:
        if self.should_fail:
            raise PoolProcessError("forced spawn failure")
        self.healthy = True
        self._alive = True
        self.last_used = time.monotonic()

    def is_alive(self) -> bool:
        return self._alive

    def invoke(self, prompt: str, *, timeout_seconds: float) -> InvokeResult:
        self.message_count += 1
        self.last_used = time.monotonic()
        return InvokeResult(
            text="ok",
            session_id="sid",
            stop_reason="end_turn",
            is_error=False,
            error_text=None,
            raw_events=(),
        )

    def terminate(self, *, grace_seconds: float = 2.0) -> None:
        self.terminated = True
        self.healthy = False
        self._alive = False


class PoolManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        FakePoolProcess.spawn_count = 0
        self._members: list[FakePoolProcess] = []

    def _factory(self, key: tuple[str, str, str | None]) -> FakePoolProcess:
        member = FakePoolProcess(key=key)
        self._members.append(member)
        return member  # type: ignore[return-value]

    def test_get_or_create_caches(self) -> None:
        pool = PoolManager(self._factory, max_size=3, idle_timeout_seconds=300)
        m1 = pool.get_or_create(("c1", "claude", None))
        m2 = pool.get_or_create(("c1", "claude", None))
        self.assertIs(m1, m2)
        self.assertEqual(FakePoolProcess.spawn_count, 1)
        stats = pool.stats()
        self.assertEqual(stats.hits, 1)
        self.assertEqual(stats.misses, 1)

    def test_distinct_keys_get_distinct_members(self) -> None:
        pool = PoolManager(self._factory, max_size=3, idle_timeout_seconds=300)
        m1 = pool.get_or_create(("c1", "claude", None))
        m2 = pool.get_or_create(("c2", "claude", None))
        m3 = pool.get_or_create(("c1", "claude", "haiku"))
        self.assertIsNot(m1, m2)
        self.assertIsNot(m1, m3)
        self.assertEqual(len(pool), 3)

    def test_capacity_evicts_lru(self) -> None:
        pool = PoolManager(self._factory, max_size=2, idle_timeout_seconds=300)
        m1 = pool.get_or_create(("c1", "claude", None))
        time.sleep(0.01)
        m2 = pool.get_or_create(("c2", "claude", None))
        time.sleep(0.01)
        # Touch m2 so m1 is the LRU.
        pool.release(("c2", "claude", None))
        time.sleep(0.01)
        m3 = pool.get_or_create(("c3", "claude", None))
        self.assertEqual(len(pool), 2)
        self.assertTrue(m1.terminated)
        self.assertFalse(m2.terminated)
        self.assertIs(m3, self._members[-1])

    def test_dead_member_is_replaced(self) -> None:
        pool = PoolManager(self._factory, max_size=3, idle_timeout_seconds=300)
        m1 = pool.get_or_create(("c1", "claude", None))
        m1._alive = False  # simulate process death
        m2 = pool.get_or_create(("c1", "claude", None))
        self.assertIsNot(m1, m2)
        self.assertTrue(m1.terminated)
        self.assertEqual(FakePoolProcess.spawn_count, 2)

    def test_evict_idle(self) -> None:
        pool = PoolManager(self._factory, max_size=3, idle_timeout_seconds=0.05)
        pool.get_or_create(("c1", "claude", None))
        pool.get_or_create(("c2", "claude", None))
        time.sleep(0.1)
        evicted = pool.evict_idle()
        self.assertEqual(evicted, 2)
        self.assertEqual(len(pool), 0)

    def test_factory_failure_propagates_and_does_not_register(self) -> None:
        def bad_factory(_key):
            return FakePoolProcess(key=_key, should_fail=True)

        pool = PoolManager(bad_factory, max_size=3, idle_timeout_seconds=300)
        with self.assertRaises(PoolProcessError):
            pool.get_or_create(("c1", "claude", None))
        self.assertEqual(len(pool), 0)

    def test_shutdown_terminates_all(self) -> None:
        pool = PoolManager(self._factory, max_size=3, idle_timeout_seconds=300)
        pool.get_or_create(("c1", "claude", None))
        pool.get_or_create(("c2", "claude", None))
        self.assertEqual(len(pool), 2)
        pool.shutdown()
        self.assertEqual(len(pool), 0)
        for m in self._members:
            self.assertTrue(m.terminated)

    def test_evict_explicit(self) -> None:
        pool = PoolManager(self._factory, max_size=3, idle_timeout_seconds=300)
        pool.get_or_create(("c1", "claude", None))
        self.assertTrue(pool.evict(("c1", "claude", None)))
        self.assertFalse(pool.evict(("c1", "claude", None)))
        self.assertEqual(len(pool), 0)


class WarmPoolConfigTests(unittest.TestCase):
    def test_default_disabled(self) -> None:
        from gateway.config import WarmPoolConfig

        cfg = WarmPoolConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.max_size, 20)
        self.assertEqual(cfg.idle_timeout_seconds, 300)

    def test_load_from_yaml_dict(self) -> None:
        from gateway.config import _load_warm_pool

        cfg = _load_warm_pool(
            {
                "warm_pool": {
                    "enabled": True,
                    "max_size": 5,
                    "idle_timeout_seconds": 120,
                }
            }
        )
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.max_size, 5)
        self.assertEqual(cfg.idle_timeout_seconds, 120)
        self.assertEqual(cfg.startup_timeout_seconds, 30)

    def test_load_missing_block(self) -> None:
        from gateway.config import _load_warm_pool

        cfg = _load_warm_pool({})
        self.assertFalse(cfg.enabled)


if __name__ == "__main__":
    unittest.main()
