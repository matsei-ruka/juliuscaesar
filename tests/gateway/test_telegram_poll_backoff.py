"""getUpdates 409/429 backoff (audit Finding F — regression from 42582e1).

`http_json` returns API error bodies as parsed JSON (correct for the 400
no-op-edit dedup), so a 409 (token bleed) or 429 reaches the poll loop as
`{"ok": false}` instead of raising. Without an explicit `ok` check the loop
re-polled instantly and invisibly. The loop must back off exponentially,
honor `retry_after`, log 409 loudly, and reset the streak on success.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.channels import telegram as telegram_module  # noqa: E402
from gateway.channels.telegram import TelegramChannel  # noqa: E402
from gateway.config import ChannelConfig  # noqa: E402


def _channel(tmp: Path) -> TelegramChannel:
    cfg = ChannelConfig(enabled=True, token_env="TELEGRAM_BOT_TOKEN", chat_ids=[])
    channel = TelegramChannel(tmp, cfg, lambda _msg: None)
    channel.token = "test-token"
    return channel


class _DriveDone(BaseException):
    """Ends the poll loop from inside the fake http_json.

    BaseException on purpose: the loop body catches `Exception` (and would
    real-sleep 5s); a control signal must pass through that handler.
    """


def _drive(channel, responses, max_polls=None):
    """Run the poll loop over canned getUpdates bodies; capture sleeps + logs."""
    max_polls = max_polls if max_polls is not None else len(responses)
    state = {"polls": 0}
    sleeps: list[float] = []
    logs: list[str] = []
    channel.log = logs.append

    def fake_http_json(url, **kwargs):
        if "getUpdates" in url:
            if state["polls"] >= max_polls:
                raise _DriveDone()
            body = responses[min(state["polls"], len(responses) - 1)]
            state["polls"] += 1
            return body
        return {"ok": True, "result": {}}

    def fake_sleep(seconds, should_stop):
        sleeps.append(seconds)

    orig_http = telegram_module.http_json
    orig_sleep = TelegramChannel._interruptible_sleep
    telegram_module.http_json = fake_http_json
    TelegramChannel._interruptible_sleep = staticmethod(fake_sleep)
    try:
        channel.run(enqueue=lambda **kw: None, should_stop=lambda: False)
    except _DriveDone:
        pass
    finally:
        telegram_module.http_json = orig_http
        TelegramChannel._interruptible_sleep = orig_sleep
    return sleeps, logs


class PollBackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc-pollbackoff-"))

    def test_409_backs_off_exponentially_and_logs_conflict(self) -> None:
        body = {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"}
        channel = _channel(self.tmp)
        sleeps, logs = _drive(channel, [body, body, body], max_polls=3)
        self.assertEqual(sleeps, [5.0, 10.0, 20.0])
        conflict_lines = [line for line in logs if "conflict (409)" in line]
        self.assertEqual(len(conflict_lines), 3)
        self.assertIn("token bleed", conflict_lines[0])

    def test_429_honors_retry_after_over_computed_backoff(self) -> None:
        body = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 37",
            "parameters": {"retry_after": 37},
        }
        channel = _channel(self.tmp)
        sleeps, logs = _drive(channel, [body], max_polls=1)
        self.assertEqual(sleeps, [37.0])
        self.assertTrue(any("error_code=429" in line for line in logs))

    def test_streak_resets_on_ok_response(self) -> None:
        not_ok = {"ok": False, "error_code": 409, "description": "Conflict"}
        ok = {"ok": True, "result": []}
        channel = _channel(self.tmp)
        sleeps, _logs = _drive(channel, [not_ok, not_ok, ok, not_ok], max_polls=4)
        # 5, 10 → ok resets → 5 again (not 20).
        self.assertEqual(sleeps, [5.0, 10.0, 5.0])

    def test_backoff_caps_at_300s(self) -> None:
        body = {"ok": False, "error_code": 409, "description": "Conflict"}
        channel = _channel(self.tmp)
        sleeps, _logs = _drive(channel, [body] * 10, max_polls=10)
        self.assertEqual(max(sleeps), 300.0)
        self.assertEqual(sleeps[-1], 300.0)

    def test_ok_false_never_tight_loops(self) -> None:
        # Every not-ok poll must be followed by a sleep — the regression was
        # zero sleeps between instant re-polls.
        body = {"ok": False, "error_code": 429, "description": "Too Many Requests"}
        channel = _channel(self.tmp)
        sleeps, _logs = _drive(channel, [body] * 5, max_polls=5)
        self.assertEqual(len(sleeps), 5)
        self.assertTrue(all(s >= 5.0 for s in sleeps))


if __name__ == "__main__":
    unittest.main()
