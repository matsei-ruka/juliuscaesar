"""Fixtures for user_model tests."""

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lib.gateway.queue import connect as queue_connect


@pytest.fixture
def instance_dir_with_queue():
    """Create a temp instance dir with a populated queue.db."""
    with tempfile.TemporaryDirectory() as tmpdir:
        instance = Path(tmpdir)
        (instance / "state" / "gateway").mkdir(parents=True)
        (instance / "memory" / "L1").mkdir(parents=True)

        # Create queue.db and populate with test events.
        conn = queue_connect(instance)

        now = datetime.now(timezone.utc)
        seven_days_ago = now - timedelta(days=7)

        # Insert test events.
        for i in range(5):
            ts = (seven_days_ago + timedelta(days=i)).isoformat(timespec="seconds").replace("+00:00", "Z")
            conn.execute(
                """
                INSERT INTO events (source, user_id, conversation_id, content, response, status, received_at, available_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("telegram", "user_123", "conv_456", f"Message {i}: Luca met Martina today", f"Response {i}", "finished", ts, ts),
            )

        conn.commit()
        conn.close()

        yield instance


@pytest.fixture
def sample_user_md():
    """Sample USER.md content."""
    return """---
title: User
---

## Family
- Daughter [[people/martina|Martina]] — 6yo

## Communication preferences
- Direct style, no fluff
"""
