"""Tests for Phase 3 of supervisor card actions (polish).

Covers:
  - Debouncing: second press within 2s is silently no-op'd.
  - Audit log: actor_chat_id field present in every record.
  - stop_session now writes an audit record.
  - audit_background_done writes a background_done record.
  - jc actions list CLI reads the log and detects leaked sessions.
  - jc doctor Actions section warns on leaked backgrounded sessions.

Avoids network I/O — Telegram HTTP fully mocked.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import actions, actions_registry  # noqa: E402
from gateway import config as gateway_config  # noqa: E402
from gateway import queue  # noqa: E402
from gateway.channels.telegram import TelegramChannel  # noqa: E402
from gateway.config import ChannelConfig  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_instance(allowed_chat_ids: list[str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="actions-p3-"))
    (root / "ops").mkdir()
    (root / "state").mkdir()
    chat_ids_list = ", ".join(allowed_chat_ids)
    (root / "ops" / "gateway.yaml").write_text(
        "default_brain: claude\n"
        "channels:\n"
        "  telegram:\n"
        "    enabled: true\n"
        "    token_env: TELEGRAM_BOT_TOKEN\n"
        f"    chat_ids: [{chat_ids_list}]\n"
        "actions:\n"
        "  enabled: true\n"
        "  stop_grace_seconds: 2\n"
        "  max_background_per_chat: 3\n"
    )
    (root / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token-xyz\n"
        f"TELEGRAM_CHAT_ID='{allowed_chat_ids[0]}'\n"
    )
    queue.connect(root)
    gateway_config.clear_config_cache()
    gateway_config.clear_env_cache()
    return root


def _make_channel(instance: Path, allowed_chat_ids: list[str]) -> TelegramChannel:
    cfg = ChannelConfig(
        enabled=True,
        chat_ids=tuple(allowed_chat_ids),
        token_env="TELEGRAM_BOT_TOKEN",
    )
    return TelegramChannel(instance_dir=instance, cfg=cfg, log=MagicMock())


def _read_audit(instance: Path) -> list[dict]:
    path = instance / "state" / "actions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Debouncing
# ---------------------------------------------------------------------------


class DebounceTests(unittest.TestCase):
    def setUp(self) -> None:
        actions_registry.clear()

    def tearDown(self) -> None:
        actions_registry.clear()

    def test_first_press_not_debounced(self) -> None:
        sid = "a" * 32
        result = actions_registry.check_and_set_debounce(sid, window_seconds=2.0)
        self.assertFalse(result, "first press should NOT be debounced")

    def test_second_press_within_window_debounced(self) -> None:
        sid = "b" * 32
        actions_registry.check_and_set_debounce(sid, window_seconds=2.0)
        result = actions_registry.check_and_set_debounce(sid, window_seconds=2.0)
        self.assertTrue(result, "second press within 2s SHOULD be debounced")

    def test_press_after_window_not_debounced(self) -> None:
        sid = "c" * 32
        actions_registry.check_and_set_debounce(sid, window_seconds=0.05)
        time.sleep(0.1)
        result = actions_registry.check_and_set_debounce(sid, window_seconds=0.05)
        self.assertFalse(result, "press after window should NOT be debounced")

    def test_unregister_clears_debounce(self) -> None:
        sid = "d" * 32
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=1,
            slot_id=0,
        )
        actions_registry.check_and_set_debounce(sid, window_seconds=60.0)
        # Second press debounced while registered
        self.assertTrue(actions_registry.check_and_set_debounce(sid, window_seconds=60.0))
        actions_registry.unregister(sid)
        # After unregister, debounce clock cleared — next press is fresh
        self.assertFalse(actions_registry.check_and_set_debounce(sid, window_seconds=60.0))

    def test_empty_session_id_not_debounced(self) -> None:
        self.assertFalse(actions_registry.check_and_set_debounce(""))

    def test_debounce_per_session_independent(self) -> None:
        sid1, sid2 = "e" * 32, "f" * 32
        actions_registry.check_and_set_debounce(sid1, window_seconds=5.0)
        # sid1 is debounced; sid2 is not
        self.assertTrue(actions_registry.check_and_set_debounce(sid1, window_seconds=5.0))
        self.assertFalse(actions_registry.check_and_set_debounce(sid2, window_seconds=5.0))


# ---------------------------------------------------------------------------
# Telegram callback debounce integration
# ---------------------------------------------------------------------------


class TelegramDebounceCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        actions_registry.clear()

    def tearDown(self) -> None:
        actions_registry.clear()
        gateway_config.clear_config_cache()
        gateway_config.clear_env_cache()

    def test_stop_debounced_second_tap_silently_answered(self) -> None:
        instance = _make_instance(allowed_chat_ids=["28547271"])
        channel = _make_channel(instance, ["28547271"])
        try:
            sid = "g" * 32
            actions_registry.register(
                short_token=sid[:12],
                session_id=sid,
                child_pid=1,
                slot_id=0,
                chat_id="28547271",
            )
            update = {
                "callback_query": {
                    "id": "cq-db1",
                    "data": f"act:stop:{sid[:12]}",
                    "from": {"id": 28547271},
                    "message": {
                        "message_id": 10,
                        "chat": {"id": 28547271},
                        "text": "🛠️ task",
                    },
                }
            }
            stop_calls: list[str] = []

            def fake_stop(sid_arg, **_kw):
                stop_calls.append(sid_arg)
                actions_registry.mark_stopped(sid_arg)
                return actions.StopResult(ok=True, already_stopped=False,
                                          elapsed_ms=5, reason="sigterm")

            with patch("gateway.channels.telegram.http_json") as mock_http, \
                 patch("gateway.actions.stop_session", side_effect=fake_stop):
                mock_http.return_value = {"ok": True}
                # First tap — real stop
                channel._handle_callback_query(update)
                self.assertEqual(len(stop_calls), 1)
                first_calls = mock_http.call_count
                # Second tap immediately — debounced
                channel._handle_callback_query(update)
                # stop_session still called only once (debounced)
                self.assertEqual(len(stop_calls), 1)
                # Only one extra http call (the silent answer)
                extra_calls = mock_http.call_count - first_calls
                self.assertEqual(extra_calls, 1)
        finally:
            channel.close()

    def test_background_debounced_second_tap_silently_answered(self) -> None:
        instance = _make_instance(allowed_chat_ids=["28547271"])
        channel = _make_channel(instance, ["28547271"])
        try:
            sid = "h" * 32
            actions_registry.register(
                short_token=sid[:12],
                session_id=sid,
                child_pid=1,
                slot_id=0,
                chat_id="28547271",
            )
            update = {
                "callback_query": {
                    "id": "cq-db2",
                    "data": f"act:bg:{sid[:12]}",
                    "from": {"id": 28547271},
                    "message": {
                        "message_id": 20,
                        "chat": {"id": 28547271},
                        "text": "🛠️ task",
                    },
                }
            }
            bg_calls: list[str] = []

            def fake_bg(sid_arg, **_kw):
                bg_calls.append(sid_arg)
                return actions.BackgroundResult(ok=True, already_backgrounded=False,
                                                elapsed_ms=1, reason="backgrounded")

            with patch("gateway.channels.telegram.http_json") as mock_http, \
                 patch("gateway.actions.background_session", side_effect=fake_bg):
                mock_http.return_value = {"ok": True}
                channel._handle_callback_query(update)
                self.assertEqual(len(bg_calls), 1)
                # Second tap — debounced
                channel._handle_callback_query(update)
                self.assertEqual(len(bg_calls), 1)
        finally:
            channel.close()


# ---------------------------------------------------------------------------
# Audit log: actor_chat_id + stop audit + background_done
# ---------------------------------------------------------------------------


class AuditLogTests(unittest.TestCase):
    def setUp(self) -> None:
        actions_registry.clear()

    def tearDown(self) -> None:
        actions_registry.clear()

    def test_background_session_audit_includes_actor_chat_id(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="audit-bg-"))
        (instance / "state").mkdir()
        sid = "i" * 32
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=1,
            slot_id=0,
            chat_id="111",
        )
        actions.background_session(
            sid,
            chat_id="111",
            instance_dir=instance,
            actor_chat_id="999",
        )
        records = _read_audit(instance)
        self.assertTrue(records, "audit log should have at least one record")
        rec = records[-1]
        self.assertEqual(rec.get("verb"), "background")
        self.assertEqual(rec.get("actor_chat_id"), "999")
        self.assertIn("session_id", rec)
        self.assertIn("chat_id", rec)
        self.assertIn("ts", rec)

    def test_stop_session_writes_audit_record(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="audit-stop-"))
        (instance / "state").mkdir()
        sid = "j" * 32
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=999999,  # non-existent PID → pid_gone fast path
            slot_id=0,
            chat_id="222",
        )
        proc = subprocess.Popen(["sleep", "0.01"], start_new_session=True)
        proc.wait()
        actions_registry.clear()
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=proc.pid,  # dead pid → pid_gone
            slot_id=0,
            chat_id="222",
        )
        actions.stop_session(
            sid,
            stop_grace_seconds=1,
            instance_dir=instance,
            actor_chat_id="777",
        )
        records = _read_audit(instance)
        self.assertTrue(records, "stop should write an audit record")
        stop_records = [r for r in records if r.get("verb") == "stop"]
        self.assertTrue(stop_records, "should have a stop verb record")
        rec = stop_records[-1]
        self.assertEqual(rec.get("actor_chat_id"), "777")
        self.assertEqual(rec.get("chat_id"), "222")

    def test_stop_not_found_still_audits(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="audit-stop-nf-"))
        (instance / "state").mkdir()
        sid = "k" * 32
        actions.stop_session(sid, instance_dir=instance, actor_chat_id="555")
        records = _read_audit(instance)
        self.assertTrue(records)
        rec = records[-1]
        self.assertEqual(rec["verb"], "stop")
        self.assertEqual(rec["result"]["reason"], "not_found")

    def test_audit_background_done_writes_record(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="audit-done-"))
        (instance / "state").mkdir()
        sid = "l" * 32
        actions.audit_background_done(instance, sid, "333", duration_s=45.5, reason="done")
        records = _read_audit(instance)
        self.assertTrue(records)
        rec = records[-1]
        self.assertEqual(rec["verb"], "background_done")
        self.assertEqual(rec["session_id"], sid)
        self.assertEqual(rec["chat_id"], "333")
        self.assertAlmostEqual(rec["result"]["duration_s"], 45.5, places=0)

    def test_audit_record_full_shape(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="audit-shape-"))
        (instance / "state").mkdir()
        sid = "m" * 32
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=1,
            slot_id=0,
            chat_id="444",
        )
        actions.background_session(
            sid,
            chat_id="444",
            instance_dir=instance,
            actor_chat_id="28547271",
        )
        records = _read_audit(instance)
        rec = records[-1]
        for field in ("ts", "session_id", "chat_id", "verb", "actor_chat_id", "result"):
            self.assertIn(field, rec, f"missing field: {field}")


# ---------------------------------------------------------------------------
# jc actions list CLI
# ---------------------------------------------------------------------------


class ActionsListCLITests(unittest.TestCase):
    def _run_actions_list(self, instance: Path) -> subprocess.CompletedProcess:
        cli = REPO_ROOT / "bin" / "jc-actions"
        env = {"JC_INSTANCE_DIR": str(instance), "PATH": "/usr/bin:/bin",
               "HOME": str(Path.home())}
        return subprocess.run(
            [str(cli), "list"],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_no_log_file_exits_cleanly(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="act-list-"))
        (instance / "state").mkdir()
        result = self._run_actions_list(instance)
        self.assertEqual(result.returncode, 0)
        self.assertIn("No actions log", result.stdout)

    def test_active_backgrounded_session_shown(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="act-list-active-"))
        (instance / "state").mkdir()
        sid = "n" * 32
        # Write a backgrounded record (no done record → active)
        log_path = instance / "state" / "actions.jsonl"
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        rec = {
            "ts": ts, "session_id": sid, "chat_id": "111",
            "verb": "background", "actor_chat_id": "28547271",
            "result": {"ok": True, "already_backgrounded": False, "reason": "backgrounded"},
        }
        log_path.write_text(json.dumps(rec) + "\n")
        result = self._run_actions_list(instance)
        self.assertEqual(result.returncode, 0)
        self.assertIn(sid[:14], result.stdout)
        self.assertIn("running", result.stdout)

    def test_completed_session_not_shown(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="act-list-done-"))
        (instance / "state").mkdir()
        sid = "o" * 32
        log_path = instance / "state" / "actions.jsonl"
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        bg_rec = {
            "ts": ts, "session_id": sid, "chat_id": "111",
            "verb": "background", "actor_chat_id": "28547271",
            "result": {"ok": True, "already_backgrounded": False, "reason": "backgrounded"},
        }
        done_rec = {
            "ts": ts, "session_id": sid, "chat_id": "111",
            "verb": "background_done", "actor_chat_id": "",
            "result": {"duration_s": 10.0, "reason": "done"},
        }
        log_path.write_text(
            json.dumps(bg_rec) + "\n" + json.dumps(done_rec) + "\n"
        )
        result = self._run_actions_list(instance)
        self.assertEqual(result.returncode, 0)
        self.assertIn("No active", result.stdout)

    def test_leaked_session_marked_leaked(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="act-list-leak-"))
        (instance / "state").mkdir()
        sid = "p" * 32
        log_path = instance / "state" / "actions.jsonl"
        # ts far in the past (well past any threshold)
        from datetime import datetime, timezone
        old_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        rec = {
            "ts": old_ts, "session_id": sid, "chat_id": "111",
            "verb": "background", "actor_chat_id": "28547271",
            "result": {"ok": True, "already_backgrounded": False, "reason": "backgrounded"},
        }
        log_path.write_text(json.dumps(rec) + "\n")
        result = self._run_actions_list(instance)
        self.assertEqual(result.returncode, 0)
        self.assertIn("LEAKED", result.stdout)


# ---------------------------------------------------------------------------
# jc doctor Actions section
# ---------------------------------------------------------------------------


class DoctorActionsCheckTests(unittest.TestCase):
    def _run_doctor(self, instance: Path) -> subprocess.CompletedProcess:
        doctor = REPO_ROOT / "bin" / "jc-doctor"
        env = {"JC_INSTANCE_DIR": str(instance), "PATH": "/usr/bin:/bin",
               "HOME": str(Path.home())}
        return subprocess.run(
            [str(doctor), "--instance-dir", str(instance)],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_no_log_emits_info(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="dr-act-"))
        # Minimal instance: just ops/gateway.yaml + .env so doctor can parse config
        (instance / "ops").mkdir()
        (instance / "ops" / "gateway.yaml").write_text(
            "default_brain: claude\n"
            "channels:\n"
            "  telegram:\n"
            "    enabled: false\n"
            "    token_env: TELEGRAM_BOT_TOKEN\n"
            "    chat_ids: []\n"
        )
        (instance / ".env").write_text("TELEGRAM_BOT_TOKEN=test\n")
        result = self._run_doctor(instance)
        combined = result.stdout + result.stderr
        # Doctor should mention "Actions" section and the no-log info
        self.assertIn("Actions", combined)
        self.assertIn("not yet created", combined)

    def test_leaked_session_produces_warn(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="dr-leak-"))
        (instance / "ops").mkdir()
        (instance / "state").mkdir()
        (instance / "ops" / "gateway.yaml").write_text(
            "default_brain: claude\n"
            "channels:\n"
            "  telegram:\n"
            "    enabled: false\n"
            "    token_env: TELEGRAM_BOT_TOKEN\n"
            "    chat_ids: []\n"
            "actions:\n"
            "  enabled: true\n"
            "  stop_grace_seconds: 5\n"
        )
        (instance / ".env").write_text("TELEGRAM_BOT_TOKEN=test\n")
        sid = "q" * 32
        log_path = instance / "state" / "actions.jsonl"
        from datetime import datetime, timezone
        old_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        rec = {
            "ts": old_ts, "session_id": sid, "chat_id": "111",
            "verb": "background", "actor_chat_id": "28547271",
            "result": {"ok": True, "already_backgrounded": False, "reason": "backgrounded"},
        }
        log_path.write_text(json.dumps(rec) + "\n")
        result = self._run_doctor(instance)
        combined = result.stdout + result.stderr
        self.assertIn("Actions", combined)
        self.assertTrue(
            "leaked" in combined.lower() or "WARN" in combined or "!" in combined,
            f"Expected leak warning in doctor output.\nstdout={result.stdout}\nstderr={result.stderr}",
        )

    def test_no_leaked_sessions_is_ok(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="dr-ok-"))
        (instance / "ops").mkdir()
        (instance / "state").mkdir()
        (instance / "ops" / "gateway.yaml").write_text(
            "default_brain: claude\n"
            "channels:\n"
            "  telegram:\n"
            "    enabled: false\n"
            "    token_env: TELEGRAM_BOT_TOKEN\n"
            "    chat_ids: []\n"
        )
        (instance / ".env").write_text("TELEGRAM_BOT_TOKEN=test\n")
        sid = "r" * 32
        log_path = instance / "state" / "actions.jsonl"
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        bg_rec = {
            "ts": ts, "session_id": sid, "chat_id": "111",
            "verb": "background", "actor_chat_id": "28547271",
            "result": {"ok": True, "already_backgrounded": False, "reason": "backgrounded"},
        }
        done_rec = {
            "ts": ts, "session_id": sid, "chat_id": "111",
            "verb": "background_done", "actor_chat_id": "",
            "result": {"duration_s": 30.0, "reason": "done"},
        }
        log_path.write_text(
            json.dumps(bg_rec) + "\n" + json.dumps(done_rec) + "\n"
        )
        result = self._run_doctor(instance)
        combined = result.stdout + result.stderr
        self.assertIn("Actions", combined)
        # Should see "✓" or OK for the actions section (no leak)
        self.assertTrue(
            "✓" in combined or "OK" in combined or "no backgrounded" in combined.lower(),
            f"Expected OK for actions in doctor output.\nstdout={result.stdout}",
        )


# ---------------------------------------------------------------------------
# Cross-process registry persistence (disk shadow)
# ---------------------------------------------------------------------------


class DiskPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        actions_registry.clear()

    def tearDown(self) -> None:
        actions_registry.clear()

    def test_register_writes_event_file(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="disk-reg-"))
        sid = "s" * 32
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=1234,
            slot_id=0,
            chat_id="111",
            event_id=42,
            instance_dir=instance,
        )
        event_file = instance / "state" / "actions" / "event-42.json"
        self.assertTrue(event_file.exists(), "event file should be written on register")
        data = json.loads(event_file.read_text())
        self.assertEqual(data["session_id"], sid)
        self.assertEqual(data["short_token"], sid[:12])
        self.assertEqual(data["event_id"], 42)

    def test_unregister_removes_event_file(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="disk-unreg-"))
        sid = "t" * 32
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=1,
            slot_id=0,
            event_id=99,
            instance_dir=instance,
        )
        event_file = instance / "state" / "actions" / "event-99.json"
        self.assertTrue(event_file.exists())
        actions_registry.unregister(sid, instance_dir=instance)
        self.assertFalse(event_file.exists(), "event file should be removed on unregister")

    def test_resolve_by_event_with_disk_cross_process(self) -> None:
        """Simulate cross-process lookup: write entry, clear memory, read from disk."""
        instance = Path(tempfile.mkdtemp(prefix="disk-xproc-"))
        sid = "u" * 32
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=999,
            slot_id=0,
            chat_id="222",
            event_id=77,
            instance_dir=instance,
        )
        # Simulate another process: clear in-mem state.
        actions_registry.clear()
        entry = actions_registry.resolve_by_event_with_disk(instance, 77)
        self.assertIsNotNone(entry, "should fall back to disk-loaded entry")
        self.assertEqual(entry.session_id, sid)
        self.assertEqual(entry.short_token, sid[:12])
        self.assertEqual(entry.chat_id, "222")
        self.assertFalse(entry.stopped)

    def test_resolve_with_disk_returns_inmem_when_present(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="disk-prefer-mem-"))
        sid = "v" * 32
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=42,
            slot_id=0,
            event_id=55,
            instance_dir=instance,
        )
        entry = actions_registry.resolve_by_event_with_disk(instance, 55)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.child_pid, 42)

    def test_resolve_with_disk_returns_none_when_missing(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="disk-empty-"))
        entry = actions_registry.resolve_by_event_with_disk(instance, 1234)
        self.assertIsNone(entry)

    def test_mark_stopped_persists_to_disk(self) -> None:
        instance = Path(tempfile.mkdtemp(prefix="disk-stop-"))
        sid = "w" * 32
        actions_registry.register(
            short_token=sid[:12],
            session_id=sid,
            child_pid=1,
            slot_id=0,
            event_id=33,
            instance_dir=instance,
        )
        actions_registry.mark_stopped(sid, instance_dir=instance)
        actions_registry.clear()
        entry = actions_registry.resolve_by_event_with_disk(instance, 33)
        self.assertIsNotNone(entry)
        self.assertTrue(entry.stopped, "stopped flag should survive cross-process")


if __name__ == "__main__":
    unittest.main()
