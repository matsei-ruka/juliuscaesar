"""Tests for `health.accountabilities_check.check_accountabilities`.

Covers docs/specs/accountabilities.md §Phase 5:
- Disabled returns a single INFO item.
- Enabled: manifest missing → warn; manifest present → ok.
- Enabled: RULES.md missing the constitutional section → warn; present → ok.
- Enabled: audit log missing → warn; present → ok.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import config as gateway_config  # noqa: E402
from health.accountabilities_check import check_accountabilities  # noqa: E402


MANIFEST_BODY = """---
slug: accountabilities-manifest
title: Test — Accountability Manifest
layer: L1
type: manifest
state: active
version: 1.0.0
---

# Manifest

## Active accountabilities
"""

RULES_BODY = """# RULES

## §1 — Accountability Principle

Inside / Adjacent / Outside / Delegated apply per request.
"""


def _enable_config(instance: Path, *, channel: str = "telegram-primary") -> None:
    (instance / "ops").mkdir(exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(
        "accountabilities:\n"
        "  enabled: true\n"
        f"  authority_channel: {channel}\n",
        encoding="utf-8",
    )
    gateway_config.clear_config_cache()


def _disable_config(instance: Path) -> None:
    (instance / "ops").mkdir(exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(
        "accountabilities:\n  enabled: false\n",
        encoding="utf-8",
    )
    gateway_config.clear_config_cache()


def _write_manifest(instance: Path, body: str = MANIFEST_BODY) -> None:
    target = instance / "memory" / "L1" / "accountabilities-manifest.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _write_rules(instance: Path, body: str = RULES_BODY) -> None:
    target = instance / "memory" / "L1" / "RULES.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _write_audit(instance: Path) -> None:
    target = instance / "memory" / "L2" / "accountabilities" / "_audit.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nslug: accountabilities-audit\n---\n", encoding="utf-8")


class CheckAccountabilitiesTests(unittest.TestCase):
    def setUp(self) -> None:
        gateway_config.clear_config_cache()

    def tearDown(self) -> None:
        gateway_config.clear_config_cache()

    def test_disabled_returns_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _disable_config(instance)
            items = check_accountabilities(instance)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].level, "info")
            self.assertIn("disabled", items[0].message)

    def test_manifest_missing_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            items = check_accountabilities(instance)
            manifest_items = [i for i in items if "manifest" in i.message]
            self.assertTrue(manifest_items, f"no manifest item in {items}")
            self.assertEqual(manifest_items[0].level, "warn")

    def test_manifest_present_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_manifest(instance)
            items = check_accountabilities(instance)
            manifest_items = [i for i in items if "manifest" in i.message]
            self.assertTrue(manifest_items)
            self.assertEqual(manifest_items[0].level, "ok")

    def test_rules_missing_section_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_manifest(instance)
            _write_rules(instance, body="# RULES\n\nNo accountability section here.\n")
            items = check_accountabilities(instance)
            rules_items = [i for i in items if "RULES" in i.message]
            self.assertTrue(rules_items)
            self.assertEqual(rules_items[0].level, "warn")

    def test_rules_with_section_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_manifest(instance)
            _write_rules(instance)
            items = check_accountabilities(instance)
            rules_items = [i for i in items if "RULES" in i.message]
            self.assertTrue(rules_items)
            self.assertEqual(rules_items[0].level, "ok")

    def test_audit_missing_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_manifest(instance)
            _write_rules(instance)
            items = check_accountabilities(instance)
            audit_items = [i for i in items if "audit" in i.message]
            self.assertTrue(audit_items)
            self.assertEqual(audit_items[0].level, "warn")

    def test_audit_present_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_manifest(instance)
            _write_rules(instance)
            _write_audit(instance)
            items = check_accountabilities(instance)
            audit_items = [i for i in items if "audit" in i.message]
            self.assertTrue(audit_items)
            self.assertEqual(audit_items[0].level, "ok")


if __name__ == "__main__":
    unittest.main()
