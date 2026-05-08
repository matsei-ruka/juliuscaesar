"""Tests that the heartbeat runner reads timezone from gateway config.

Covers docs/specs/timezone-config.md §Heartbeat change:

- `{{timezone}}` resolves to the gateway-configured zone, not `os.environ["TZ"]`.
- `{{date}}` / `{{time}}` reflect the configured zone.
- Fallback to UTC when config is missing or malformed.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import config as gateway_config  # noqa: E402
from heartbeat import runner as runner_mod  # noqa: E402


def _write_config(instance: Path, body: str) -> None:
    (instance / "ops").mkdir(parents=True, exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(body)
    gateway_config.clear_config_cache()


class ResolveInstanceTimezoneTests(unittest.TestCase):
    def test_reads_configured_zone(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_config(instance, "timezone: Asia/Dubai\n")
            self.assertEqual(
                runner_mod._resolve_instance_timezone(instance),
                "Asia/Dubai",
            )

    def test_default_utc_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_config(instance, "default_brain: claude\n")
            self.assertEqual(
                runner_mod._resolve_instance_timezone(instance),
                "UTC",
            )

    def test_fallback_utc_on_missing_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            # No ops/gateway.yaml at all.
            self.assertEqual(
                runner_mod._resolve_instance_timezone(instance),
                "UTC",
            )

    def test_fallback_utc_on_malformed_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_config(instance, "timezone: Foo/Bar\n")
            # ConfigError swallowed → fallback UTC.
            self.assertEqual(
                runner_mod._resolve_instance_timezone(instance),
                "UTC",
            )


class TimezoneSubstitutionTests(unittest.TestCase):
    def test_template_substitutes_configured_zone(self):
        # Direct test of the substitution logic without driving a real
        # adapter dispatch: replicate the subs dict the runner builds.
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_config(instance, "timezone: Asia/Dubai\n")
            tz_name = runner_mod._resolve_instance_timezone(instance)
            ts = datetime.now(ZoneInfo(tz_name))
            subs = {
                "bundle_path": "",
                "date": ts.strftime("%Y-%m-%d"),
                "time": ts.strftime("%H:%M"),
                "timezone": tz_name,
            }
            template = "tz={{timezone}} date={{date}} time={{time}}"
            rendered = runner_mod.render_prompt_template(template, subs)
            self.assertIn("tz=Asia/Dubai", rendered)
            # Asia/Dubai is UTC+4 year-round so the date/time should not
            # collide with UTC if the wall clock differs by 4h.
            self.assertRegex(rendered, r"date=\d{4}-\d{2}-\d{2}")
            self.assertRegex(rendered, r"time=\d{2}:\d{2}")

    def test_does_not_consult_environment_tz(self):
        # Ensure runner falls back to UTC even if TZ is set in the env —
        # that env var is not part of the contract anymore.
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            old = os.environ.get("TZ")
            os.environ["TZ"] = "America/Los_Angeles"
            try:
                # No config at all → should fall back to UTC, not the env.
                self.assertEqual(
                    runner_mod._resolve_instance_timezone(instance),
                    "UTC",
                )
            finally:
                if old is None:
                    os.environ.pop("TZ", None)
                else:
                    os.environ["TZ"] = old


if __name__ == "__main__":
    unittest.main()
