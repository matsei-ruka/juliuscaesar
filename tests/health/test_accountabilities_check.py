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
from health.accountabilities_check import (  # noqa: E402
    REQUIRED_DETAIL_SECTIONS,
    _detail_has_all_sections,
    check_accountabilities,
)


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

    def test_manifest_no_frontmatter_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_manifest(instance, body="# Manifest\n\nNo frontmatter at all.\n")
            items = check_accountabilities(instance)
            manifest_items = [i for i in items if "manifest" in i.message]
            self.assertEqual(manifest_items[0].level, "warn")
            self.assertIn("no YAML frontmatter delimiters", manifest_items[0].message)

    def test_manifest_malformed_yaml_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_manifest(
                instance,
                body="---\nslug: accountabilities-manifest\n  bad: [unclosed\n---\n\n# Manifest\n",
            )
            items = check_accountabilities(instance)
            manifest_items = [i for i in items if "manifest" in i.message]
            self.assertEqual(manifest_items[0].level, "warn")
            self.assertIn("malformed YAML", manifest_items[0].message)

    def test_manifest_missing_required_fields_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_manifest(
                instance,
                body="---\nslug: accountabilities-manifest\ntitle: x\n---\n\n# Manifest\n",
            )
            items = check_accountabilities(instance)
            manifest_items = [i for i in items if "manifest" in i.message]
            self.assertEqual(manifest_items[0].level, "warn")
            self.assertIn("missing required fields", manifest_items[0].message)
            self.assertIn("layer", manifest_items[0].message)
            self.assertIn("state", manifest_items[0].message)

    def test_manifest_wrong_slug_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_manifest(
                instance,
                body=(
                    "---\nslug: wrong-slug\ntitle: x\nlayer: L1\n"
                    "type: manifest\nstate: active\nversion: 1.0.0\n---\n"
                ),
            )
            items = check_accountabilities(instance)
            manifest_items = [i for i in items if "manifest" in i.message]
            self.assertEqual(manifest_items[0].level, "warn")
            self.assertIn("slug must be", manifest_items[0].message)

    def test_manifest_invalid_state_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_manifest(
                instance,
                body=(
                    "---\nslug: accountabilities-manifest\ntitle: x\nlayer: L1\n"
                    "type: manifest\nstate: bogus\nversion: 1.0.0\n---\n"
                ),
            )
            items = check_accountabilities(instance)
            manifest_items = [i for i in items if "manifest" in i.message]
            self.assertEqual(manifest_items[0].level, "warn")
            self.assertIn("state must be", manifest_items[0].message)

    def test_manifest_wrong_layer_or_type_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_manifest(
                instance,
                body=(
                    "---\nslug: accountabilities-manifest\ntitle: x\nlayer: L2\n"
                    "type: detail\nstate: active\nversion: 1.0.0\n---\n"
                ),
            )
            items = check_accountabilities(instance)
            manifest_items = [i for i in items if "manifest" in i.message]
            self.assertEqual(manifest_items[0].level, "warn")
            self.assertIn("layer must be", manifest_items[0].message)
            self.assertIn("type must be", manifest_items[0].message)


class DetailSectionsTests(unittest.TestCase):
    def _make_body(self, sections: tuple[str, ...]) -> str:
        parts = ["# Detail file\n"]
        for s in sections:
            parts.append(f"## {s}\n\nbody\n")
        return "\n".join(parts)

    def test_all_sections_as_headings_passes(self):
        body = self._make_body(REQUIRED_DETAIL_SECTIONS)
        ok, missing = _detail_has_all_sections(body)
        self.assertTrue(ok, f"unexpectedly missing: {missing}")

    def test_headings_with_trailing_parenthetical_pass(self):
        body = (
            "## Scope (what's inside)\n\n"
            "## Out of scope (perimeter — explicit)\n\n"
            "## Outputs\n## Stakeholders\n## Cadence\n## Decision boundary\n"
            "## Adjacency notes\n## Self-check pre-action\n"
            "## Connections to existing constitution\n"
        )
        ok, missing = _detail_has_all_sections(body)
        self.assertTrue(ok, f"unexpectedly missing: {missing}")

    def test_prose_substring_does_not_count(self):
        body = (
            "# Detail\n\n"
            "This file is missing the following: Scope, Out of scope, Outputs, "
            "Stakeholders, Cadence, Decision boundary, Adjacency notes, "
            "Self-check pre-action, Connections to existing constitution.\n"
        )
        ok, missing = _detail_has_all_sections(body)
        self.assertFalse(ok)
        self.assertEqual(set(missing), set(REQUIRED_DETAIL_SECTIONS))

    def test_h1_heading_does_not_count(self):
        body = "\n".join(f"# {s}\nbody\n" for s in REQUIRED_DETAIL_SECTIONS)
        ok, missing = _detail_has_all_sections(body)
        self.assertFalse(ok)
        self.assertEqual(set(missing), set(REQUIRED_DETAIL_SECTIONS))

    def test_one_missing_heading_listed(self):
        body = self._make_body(REQUIRED_DETAIL_SECTIONS[:-1])
        ok, missing = _detail_has_all_sections(body)
        self.assertFalse(ok)
        self.assertEqual(missing, [REQUIRED_DETAIL_SECTIONS[-1]])

    def test_scope_does_not_match_out_of_scope(self):
        body = self._make_body(("Out of scope",))
        ok, missing = _detail_has_all_sections(body)
        self.assertIn("Scope", missing)


if __name__ == "__main__":
    unittest.main()
