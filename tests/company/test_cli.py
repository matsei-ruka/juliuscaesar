"""Tests for the ``jc company`` CLI."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from company import cli as company_cli  # noqa: E402
from company.reporter import Outbox  # noqa: E402
from gateway import config as gw_config  # noqa: E402


def _make_instance(tmp: str) -> Path:
    instance = Path(tmp)
    (instance / "ops").mkdir()
    (instance / "memory" / "L1").mkdir(parents=True)
    (instance / "ops" / "gateway.yaml").write_text(
        "default_brain: claude\n", encoding="utf-8"
    )
    (instance / ".env").write_text(
        "COMPANY_ENDPOINT=http://x\nCOMPANY_API_KEY=k\n", encoding="utf-8"
    )
    return instance


class ReplaySinceTests(unittest.TestCase):
    def setUp(self) -> None:
        gw_config.clear_env_cache()

    def test_replay_since_skips_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            outbox = Outbox(instance, max_mb=10, max_age_hours=24)
            outbox.append([{"event_type": "old", "payload": {}}])
            old_path = outbox.files()[0]
            backdated = time.time() - 6 * 3600
            os.utime(old_path, (backdated, backdated))

            # Add a recent file under a different day name so both coexist.
            recent_path = old_path.parent / "2999-01-01.jsonl"
            recent_path.write_text(
                '{"event_type":"new","payload":{}}\n', encoding="utf-8"
            )

            args = company_cli.build_parser().parse_args(
                ["--instance-dir", str(instance), "replay", "--since", "1h"]
            )

            with patch("company.cli.CompanyClient") as MockClient:
                fake = MagicMock()
                fake.post_events.return_value = {"accepted": 1, "rejected": []}
                MockClient.return_value = fake
                rc = company_cli.cmd_replay(args)

            self.assertEqual(rc, 0)
            self.assertEqual(fake.post_events.call_count, 1)
            # Old file untouched, recent file drained.
            self.assertTrue(old_path.exists())
            self.assertFalse(recent_path.exists())


if __name__ == "__main__":
    unittest.main()
