"""Tests for `health.entities_check.check_entities`.

Covers docs/specs/relational-awareness-layer.md §Phase 5.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import config as gateway_config  # noqa: E402
from health.entities_check import check_entities  # noqa: E402


VALID_ENTITY = """---
slug: jane-doe
entity_id: jane-doe
entity_type: human
entity_category: external_client
display_name: Jane Doe
human_authority: ""
accountabilities_pointer: TBD
knowledge_state: declared
classification_confidence: high
confidence_basis: principal stated category
created: 2026-05-15
updated: 2026-05-15
last_verified: 2026-05-15
tags: [entities, external_client]
---

# Jane Doe

Some body.
"""


def _enable_config(instance: Path) -> None:
    (instance / "ops").mkdir(exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(
        "entities:\n  enabled: true\n",
        encoding="utf-8",
    )
    gateway_config.clear_config_cache()


def _disable_config(instance: Path) -> None:
    (instance / "ops").mkdir(exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(
        "entities:\n  enabled: false\n",
        encoding="utf-8",
    )
    gateway_config.clear_config_cache()


def _write_entity(instance: Path, slug: str, body: str = VALID_ENTITY) -> None:
    target = instance / "memory" / "L2" / "entities" / f"{slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


class EntitiesCheckTests(unittest.TestCase):
    def setUp(self):
        gateway_config.clear_config_cache()

    def tearDown(self):
        gateway_config.clear_config_cache()

    def test_disabled_returns_single_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _disable_config(instance)
            items = check_entities(instance)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].level, "info")
            self.assertIn("disabled", items[0].message.lower())

    def test_enabled_missing_directory_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            items = check_entities(instance)
            self.assertTrue(any(i.level == "warn" for i in items))
            self.assertIn("entities directory missing", items[0].message)

    def test_enabled_empty_directory_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            (instance / "memory" / "L2" / "entities").mkdir(parents=True)
            items = check_entities(instance)
            self.assertTrue(
                any(
                    "no entities recorded yet" in i.message
                    for i in items
                )
            )

    def test_valid_record_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_entity(instance, "jane-doe")
            items = check_entities(instance)
            ok_items = [i for i in items if i.level == "ok"]
            self.assertTrue(
                any("jane-doe.md" in i.message for i in ok_items)
            )

    def test_invalid_category_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            body = VALID_ENTITY.replace(
                "entity_category: external_client",
                "entity_category: bogus_value",
            )
            _write_entity(instance, "jane-doe", body)
            items = check_entities(instance)
            warns = [i for i in items if i.level == "warn"]
            self.assertTrue(
                any(
                    "entity_category" in i.message and "bogus_value" in i.message
                    for i in warns
                )
            )

    def test_invalid_knowledge_state_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            body = VALID_ENTITY.replace(
                "knowledge_state: declared",
                "knowledge_state: maybe",
            )
            _write_entity(instance, "jane-doe", body)
            items = check_entities(instance)
            self.assertTrue(
                any(
                    "knowledge_state" in i.message and i.level == "warn"
                    for i in items
                )
            )

    def test_invalid_confidence_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            body = VALID_ENTITY.replace(
                "classification_confidence: high",
                "classification_confidence: extreme",
            )
            _write_entity(instance, "jane-doe", body)
            items = check_entities(instance)
            self.assertTrue(
                any(
                    "classification_confidence" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_slug_mismatch_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            body = VALID_ENTITY.replace(
                "slug: jane-doe",
                "slug: john-doe",
            )
            _write_entity(instance, "jane-doe", body)
            items = check_entities(instance)
            self.assertTrue(
                any(
                    "does not match filename stem" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_missing_required_field_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            body = "---\nslug: jane-doe\n---\n\nbody\n"
            _write_entity(instance, "jane-doe", body)
            items = check_entities(instance)
            self.assertTrue(
                any(
                    "missing required fields" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_no_frontmatter_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_entity(instance, "no-fm", "# Just a heading\n")
            items = check_entities(instance)
            self.assertTrue(
                any(
                    "no YAML frontmatter" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_underscore_files_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            entities_dir = instance / "memory" / "L2" / "entities"
            entities_dir.mkdir(parents=True)
            (entities_dir / "_categories.md").write_text("ref", encoding="utf-8")
            (entities_dir / "_README.md").write_text("readme", encoding="utf-8")
            items = check_entities(instance)
            self.assertTrue(
                any("no entities recorded yet" in i.message for i in items)
            )


if __name__ == "__main__":
    unittest.main()
