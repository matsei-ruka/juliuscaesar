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

