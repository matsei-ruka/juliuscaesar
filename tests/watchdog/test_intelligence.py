from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from gateway.config import GatewayConfig, TriageConfig, render_default_config  # noqa: E402
from gateway.recovery import state as recovery_state  # noqa: E402
from watchdog.intelligence.config import IntelligenceConfig  # noqa: E402
from watchdog.intelligence.evaluator import Evaluator  # noqa: E402
from watchdog.intelligence.models import EventSummary, Snapshot  # noqa: E402
from watchdog.intelligence.runner import run_tick  # noqa: E402
from watchdog.intelligence.state import IntelligenceState  # noqa: E402


def _instance(tmp_path: Path) -> Path:
    (tmp_path / ".jc").write_text("", encoding="utf-8")
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "gateway.yaml").write_text(
        render_default_config(telegram_enabled=True, telegram_chat_id="123"),
        encoding="utf-8",
    )
    (tmp_path / "ops" / "watchdog.yaml").write_text(
        """
watchdog:
  intelligent: true
  long_running_notice_seconds: 180
  brain_switch_enabled: true
  brain_fallbacks:
    claude: [codex]
children:
  - name: jc-gateway
    type: daemon
    enabled: true
""",
        encoding="utf-8",
    )
    return tmp_path


def _write_instance(
    tmp_path: Path,
    *,
    telegram_chat_id: str = "123",
    brain_fallbacks: str = "claude: [codex]",
) -> Path:
    (tmp_path / ".jc").write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text(
        f"TELEGRAM_CHAT_ID={telegram_chat_id}\nTELEGRAM_BOT_TOKEN=t\n",
        encoding="utf-8",
    )
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "gateway.yaml").write_text(
        render_default_config(telegram_enabled=True, telegram_chat_id=telegram_chat_id),
        encoding="utf-8",
    )
    (tmp_path / "ops" / "watchdog.yaml").write_text(
        f"""
watchdog:
  intelligent: true
  long_running_notice_seconds: 180
  brain_switch_enabled: true
  brain_fallbacks:
    {brain_fallbacks}
children:
  - name: jc-gateway
    type: daemon
    enabled: true
""",
        encoding="utf-8",
    )
    return tmp_path


def _old_ts(seconds: int) -> str:
    value = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def test_long_running_notice_is_sent_once(tmp_path: Path, monkeypatch) -> None:
    inst = _instance(tmp_path)
    conn = queue.connect(inst)
    event, _ = queue.enqueue(
        conn,
        source="telegram",
        content="please do a long thing",
        user_id="123",
        conversation_id="123",
        meta={"chat_id": "123", "brain_override": "claude"},
    )
    conn.execute(
        "UPDATE events SET status='running', started_at=?, locked_until=? WHERE id=?",
        (_old_ts(240), _old_ts(60), event.id),
    )
    conn.commit()
    conn.close()

    delivered: list[tuple[str, str, dict]] = []

    def fake_deliver(**kwargs):
        delivered.append((kwargs["source"], kwargs["response"], kwargs["meta"]))
        return "msg-1"

    monkeypatch.setattr("watchdog.intelligence.actions.deliver_response", fake_deliver)

    first = run_tick(inst)
    second = run_tick(inst)

    assert [action["action"] for action in first.actions] == ["long_running_notice"]
    assert second.actions == []
    assert len(delivered) == 1
    assert "taking a bit longer" in delivered[0][1]


def test_failed_auth_event_switches_to_fallback_brain(tmp_path: Path, monkeypatch) -> None:
    inst = _instance(tmp_path)
    conn = queue.connect(inst)
    event, _ = queue.enqueue(
        conn,
        source="telegram",
        content="please answer",
        user_id="123",
        conversation_id="123",
        meta={"chat_id": "123", "chat_type": "private", "brain_override": "claude"},
    )
    queue.fail(conn, event.id, error="authentication failed 401 unauthorized", max_retries=0)
    conn.close()

    delivered: list[str] = []
    monkeypatch.setattr(
        "watchdog.intelligence.actions.deliver_response",
        lambda **kwargs: delivered.append(kwargs["response"]) or "msg-1",
    )
    monkeypatch.setattr("watchdog.intelligence.actions._brain_validates", lambda *args: True)

    result = run_tick(inst)

    assert any(action["action"] == "brain_switch" and action["to"] == "codex" for action in result.actions)
    conn = queue.connect(inst)
    updated = queue.get(conn, event.id)
    conn.close()
    assert updated is not None
    assert updated.status == "queued"
    meta = json.loads(updated.meta or "{}")
    assert meta["brain_override"] == "codex"
    assert meta["watchdog_switch"]["from"] == "claude"
    assert any("switching this request to codex" in message for message in delivered)


def test_old_failed_auth_event_is_not_recovered(tmp_path: Path, monkeypatch) -> None:
    inst = _instance(tmp_path)
    conn = queue.connect(inst)
    event, _ = queue.enqueue(
        conn,
        source="telegram",
        content="please answer",
        user_id="123",
        conversation_id="123",
        meta={"chat_id": "123", "chat_type": "private", "brain_override": "claude"},
    )
    queue.fail(conn, event.id, error="authentication failed 401 unauthorized", max_retries=0)
    conn.execute(
        "UPDATE events SET received_at=?, started_at=? WHERE id=?",
        (_old_ts(7 * 24 * 3600), _old_ts(7 * 24 * 3600), event.id),
    )
    conn.commit()
    conn.close()

    delivered: list[str] = []
    monkeypatch.setattr(
        "watchdog.intelligence.actions.deliver_response",
        lambda **kwargs: delivered.append(kwargs["response"]) or "msg-1",
    )
    monkeypatch.setattr("watchdog.intelligence.actions._brain_validates", lambda *args: True)

    result = run_tick(inst)

    assert result.actions == []
    assert delivered == []
    conn = queue.connect(inst)
    unchanged = queue.get(conn, event.id)
    conn.close()
    assert unchanged is not None
    assert unchanged.status == "failed"
    meta = json.loads(unchanged.meta or "{}")
    assert meta["brain_override"] == "claude"
    assert "watchdog_switch" not in meta


def test_group_auth_failure_notifies_group_and_operator(tmp_path: Path, monkeypatch) -> None:
    inst = _write_instance(tmp_path, telegram_chat_id="operator", brain_fallbacks="claude: []")
    conn = queue.connect(inst)
    event, _ = queue.enqueue(
        conn,
        source="telegram",
        content="please answer",
        user_id="group-user",
        conversation_id="-100",
        meta={"chat_id": "-100", "chat_type": "supergroup", "brain_override": "claude"},
    )
    queue.fail(conn, event.id, error="session has expired; please run /login", max_retries=0)
    conn.close()

    delivered: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "watchdog.intelligence.actions.deliver_response",
        lambda **kwargs: delivered.append((kwargs["response"], kwargs["meta"])) or "msg-1",
    )

    result = run_tick(inst)

    assert any(action["action"] == "auth_pending" for action in result.actions)
    assert len(delivered) == 2
    group = [item for item in delivered if item[1]["chat_id"] == "-100"]
    operator = [item for item in delivered if item[1]["chat_id"] == "operator"]
    assert group and "notified the operator" in group[0][0]
    assert operator and "Claude authentication appears expired" in operator[0][0]
    conn = queue.connect(inst)
    pending = recovery_state.get_active_pending(conn, operator_chat="operator")
    conn.close()
    assert pending is not None
    assert pending.event_id == event.id


def test_codex_auth_failure_operator_message_uses_codex_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    inst = _write_instance(tmp_path, telegram_chat_id="operator", brain_fallbacks="codex: []")
    conn = queue.connect(inst)
    event, _ = queue.enqueue(
        conn,
        source="telegram",
        content="please answer",
        user_id="operator",
        conversation_id="operator",
        meta={"chat_id": "operator", "chat_type": "private", "brain_override": "codex"},
    )
    queue.fail(conn, event.id, error="authentication failed 401 unauthorized", max_retries=0)
    conn.close()

    delivered: list[str] = []
    monkeypatch.setattr(
        "watchdog.intelligence.actions.deliver_response",
        lambda **kwargs: delivered.append(kwargs["response"]) or "msg-1",
    )

    run_tick(inst)

    assert any("jc codex-auth refresh" in message for message in delivered)
    assert not any("claude /login" in message for message in delivered)


def test_brain_cooldown_expires(tmp_path: Path) -> None:
    state = IntelligenceState()
    state.mark_brain_unavailable(
        "claude",
        reason="auth_expired",
        until=(datetime.now(timezone.utc) - timedelta(seconds=1))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    )

    assert not state.is_brain_unavailable("claude")
    assert "claude" not in state.brain_health


def test_claude_channel_triage_backend_parses_classifier_response(
    tmp_path: Path,
    monkeypatch,
) -> None:
    inst = _write_instance(tmp_path)
    conn = queue.connect(inst)
    event, _ = queue.enqueue(
        conn,
        source="telegram",
        content="please answer",
        user_id="123",
        conversation_id="123",
        meta={"chat_id": "123", "brain_override": "claude"},
    )
    conn.close()
    summary = EventSummary(
        event=event,
        meta={"chat_id": "123", "brain_override": "claude"},
        age_seconds=240,
        brain="claude",
        status="running",
    )
    seen: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "text": json.dumps(
                        {
                            "kind": "brain_unhealthy",
                            "confidence": 0.91,
                            "severity": "warning",
                            "user_visible": True,
                            "should_switch_brain": True,
                            "summary": "classifier saw gateway errors",
                        }
                    )
                }
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(
        "watchdog.intelligence.evaluator.urllib.request.urlopen",
        fake_urlopen,
    )
    evaluator = Evaluator(
        inst,
        gateway_config=GatewayConfig(
            triage=TriageConfig(backend="claude-channel", claude_triage_port=4321)
        ),
        intelligence_config=IntelligenceConfig(use_triage_model=True),
    )

    decision = evaluator.evaluate_event(Snapshot(running=[summary]), summary)

    assert seen["url"] == "http://127.0.0.1:4321/classify"
    assert seen["timeout"] == 10
    assert "message" in seen["body"]
    assert decision.kind == "brain_unhealthy"
    assert decision.source == "triage_model"
