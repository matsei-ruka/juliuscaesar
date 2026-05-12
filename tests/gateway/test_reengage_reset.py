from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from commitments.engine import add_commitment  # noqa: E402
from commitments.schema import Commitment, parse_datetime  # noqa: E402
from gateway.queue import Event  # noqa: E402
from gateway.runtime import GatewayRuntime  # noqa: E402


def test_gateway_inbound_reply_cancels_pending_reengage_touch(tmp_path: Path) -> None:
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "reengage.yaml").write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "tracked_chats": [{"chat_id": 123, "templates": {"touch_1": "x.md"}}],
            }
        ),
        encoding="utf-8",
    )
    add_commitment(
        tmp_path,
        Commitment(
            slug="old-touch",
            created_at=parse_datetime("2026-05-11T01:00:00+00:00", field_name="created_at"),
            due_at=parse_datetime("2026-05-11T07:00:00+00:00", field_name="due_at"),
            action="telegram-send",
            chat_id=123,
            text="Checking in.",
            tags=("re-engagement", "re-engagement:123", "touch:1"),
            origin="reengage",
            metadata={"retries": 0},
        ),
    )
    runtime = object.__new__(GatewayRuntime)
    runtime.instance_dir = tmp_path
    runtime.log = lambda *args, **kwargs: None
    event = Event(
        id=1,
        source="telegram",
        source_message_id="m1",
        user_id="123",
        conversation_id="123",
        content="I'm back",
        meta=json.dumps({"chat_id": "123"}),
        status="running",
        received_at=datetime.now(timezone.utc).isoformat(),
        available_at=datetime.now(timezone.utc).isoformat(),
        locked_by=None,
        locked_until=None,
        started_at=None,
        finished_at=None,
        retry_count=0,
        response=None,
        error=None,
    )

    runtime._cancel_reengage_on_inbound_reply(event)

    assert not (tmp_path / "state" / "commitments" / "old-touch.yaml").exists()
