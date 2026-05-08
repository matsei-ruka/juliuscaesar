"""Tests for the gateway-level `timezone:` config field.

Covers docs/specs/timezone-config.md §Schema change:

- `timezone: Asia/Dubai` loads cleanly and surfaces on GatewayConfig.
- `timezone:` defaults to UTC when omitted.
- Unknown IANA zones raise ConfigError.
- `timezone:` is in `allowed_top` (not rejected as unknown key).
- `render_default_config(timezone=...)` emits the line.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import config as gateway_config  # noqa: E402
from gateway.config import (  # noqa: E402
    ConfigError,
    load_config,
    render_default_config,
)


def _write_yaml(instance: Path, body: str) -> None:
    (instance / "ops").mkdir(exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(body)
    gateway_config.clear_config_cache()


class TimezoneConfigTests(unittest.TestCase):
    def test_known_zone_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "timezone: Asia/Dubai\n")
            cfg = load_config(instance)
            self.assertEqual(cfg.timezone, "Asia/Dubai")

    def test_default_is_utc(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "default_brain: claude\n")
            cfg = load_config(instance)
            self.assertEqual(cfg.timezone, "UTC")

    def test_explicit_utc(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "timezone: UTC\n")
            cfg = load_config(instance)
            self.assertEqual(cfg.timezone, "UTC")

    def test_unknown_zone_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "timezone: Foo/Bar\n")
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("timezone", str(ctx.exception))
            self.assertIn("Foo/Bar", str(ctx.exception))

    def test_empty_value_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "timezone: ''\n")
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("timezone", str(ctx.exception))

    def test_timezone_not_unknown_top_key(self):
        # Regression: validator's allowed_top must include `timezone`,
        # otherwise a bare `timezone:` key would be rejected as unknown.
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "timezone: UTC\n")
            cfg = load_config(instance)
            self.assertEqual(cfg.timezone, "UTC")

    def test_render_default_config_emits_timezone(self):
        body = render_default_config(default_brain="claude", timezone="Asia/Dubai")
        self.assertIn("timezone: Asia/Dubai", body)
        self.assertIn(
            "IANA name (e.g. Asia/Dubai)",
            body,
            msg="rendered config should carry the explanatory comment",
        )

    def test_render_default_config_default_utc(self):
        body = render_default_config(default_brain="claude")
        self.assertIn("timezone: UTC", body)


if __name__ == "__main__":
    unittest.main()
