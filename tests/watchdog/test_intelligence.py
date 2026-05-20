from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from gateway.config import render_default_config  # noqa: E402
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


def test_long_running_event_is_observed_without_user_notice(
    tmp_path: Path,
    monkeypatch,
) -> None:
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

    delivered: list[str] = []
    monkeypatch.setattr(
        "watchdog.intelligence.actions.deliver_response",
        lambda **kwargs: delivered.append(kwargs["response"]) or "msg-1",
    )

    result = run_tick(inst)

    assert result.actions == []
    assert delivered == []
    assert result.decisions == [
        {
            "kind": "long_running",
            "confidence": 0.78,
            "severity": "info",
            "user_visible": False,
            "should_switch_brain": False,
            "summary": "request is still running past the notice threshold",
            "source": "heuristic",
            "event_id": event.id,
            "brain": "claude",
            "status": "running",
        }
    ]


def test_failed_auth_event_is_ignored_by_watchdog(tmp_path: Path, monkeypatch) -> None:
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
    result = run_tick(inst)

    assert result.actions == []
    assert result.decisions == []
    assert delivered == []
    conn = queue.connect(inst)
    updated = queue.get(conn, event.id)
    conn.close()
    assert updated is not None
    assert updated.status == "failed"
    meta = json.loads(updated.meta or "{}")
    assert meta["brain_override"] == "claude"
    assert "watchdog_switch" not in meta


def test_running_auth_issue_notifies_without_replaying_event(tmp_path: Path, monkeypatch) -> None:
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
    conn.execute(
        "UPDATE events SET status='running', started_at=?, locked_until=? WHERE id=?",
        (_old_ts(240), _old_ts(60), event.id),
    )
    conn.commit()
    conn.close()
    log_dir = inst / "state" / "gateway"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "gateway.log").write_text(
        json.dumps(
            {
                "event_id": event.id,
                "brain": "claude",
                "msg": "authentication failed 401 unauthorized",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    delivered: list[str] = []
    monkeypatch.setattr(
        "watchdog.intelligence.actions.deliver_response",
        lambda **kwargs: delivered.append(kwargs["response"]) or "msg-1",
    )

    result = run_tick(inst)

    assert [action["action"] for action in result.actions] == [
        "brain_cooldown",
        "brain_issue_notice",
    ]
    assert len(delivered) == 2
    assert any("having trouble" in message for message in delivered)
    assert any("Operator action" in message for message in delivered)
    assert not any("retrying it with" in message for message in delivered)
    conn = queue.connect(inst)
    updated = queue.get(conn, event.id)
    conn.close()
    assert updated is not None
    assert updated.status == "running"
    meta = json.loads(updated.meta or "{}")
    assert meta["brain_override"] == "claude"
    assert "watchdog_switch" not in meta


def test_queued_retry_with_error_is_left_to_gateway_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
    queue.fail(conn, event.id, error="authentication failed 401 unauthorized", max_retries=3)
    conn.close()

    delivered: list[str] = []
    monkeypatch.setattr(
        "watchdog.intelligence.actions.deliver_response",
        lambda **kwargs: delivered.append(kwargs["response"]) or "msg-1",
    )

    result = run_tick(inst)

    assert result.actions == []
    assert delivered == []
    conn = queue.connect(inst)
    unchanged = queue.get(conn, event.id)
    conn.close()
    assert unchanged.status == "queued"
    meta = json.loads(unchanged.meta or "{}")
    assert meta["brain_override"] == "claude"
    assert "watchdog_switch" not in meta


def test_pi_help_text_does_not_trigger_false_auth_expired(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Regression: pi adapter help text 'Use /login to log into a provider'
    must not be classified as auth_expired for an unrelated running event."""
    inst = _instance(tmp_path)
    conn = queue.connect(inst)
    event, _ = queue.enqueue(
        conn,
        source="telegram",
        content="answer this please",
        user_id="123",
        conversation_id="123",
        meta={"chat_id": "123", "chat_type": "private", "brain_override": "claude"},
    )
    conn.execute(
        "UPDATE events SET status='running', started_at=?, locked_until=? WHERE id=?",
        (_old_ts(240), _old_ts(60), event.id),
    )
    conn.commit()
    conn.close()
    log_dir = inst / "state" / "gateway"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Simulate pi adapter help text in gateway log (no event_id) and a normal
    # dispatch line for our event (no auth markers).
    (log_dir / "gateway.log").write_text(
        "Use /login to log into a provider via OAuth or API key. See:\n"
        + json.dumps(
            {
                "event_id": event.id,
                "brain": "claude",
                "msg": f"dispatch begin id={event.id} brain=claude model=opus resume=yes",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    delivered: list[str] = []
    monkeypatch.setattr(
        "watchdog.intelligence.actions.deliver_response",
        lambda **kwargs: delivered.append(kwargs["response"]) or "msg-1",
    )

    result = run_tick(inst)

    # Must NOT cooldown the brain or notify auth issue — adapter output is
    # generic help text, not a real auth failure.
    assert "brain_cooldown" not in [a["action"] for a in result.actions]
    assert "brain_issue_notice" not in [a["action"] for a in result.actions]
    assert delivered == []


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
