"""Tests for `memory.scaffolding.scaffold_accountabilities`.

Covers docs/specs/accountabilities.md §Phase 3:
- Manifest + L2 README are copied into the instance.
- Idempotent: existing files are skipped (content preserved).
- The constitutional RULES snippet is printed (not written).
- A paste instruction is printed pointing the operator at RULES.md.
- The L2 accountabilities directory is created.
- CLAUDE.md is not patched; runtime injection is gated by ops/gateway.yaml.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from memory.scaffolding import (  # noqa: E402
    scaffold_accountabilities,
    scaffold_entities,
)


class ScaffoldAccountabilitiesTests(unittest.TestCase):
    def _scaffold(self, instance: Path) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scaffold_accountabilities(instance)
        return buf.getvalue()

    def test_scaffold_creates_manifest_and_readme(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._scaffold(instance)
            manifest = instance / "memory" / "L1" / "accountabilities-manifest.md"
            readme = instance / "memory" / "L2" / "accountabilities" / "_README.md"
            self.assertTrue(manifest.exists(), f"missing {manifest}")
            self.assertTrue(readme.exists(), f"missing {readme}")
            self.assertIn("Accountability Manifest", manifest.read_text(encoding="utf-8"))
            self.assertIn("9-section structure", readme.read_text(encoding="utf-8"))

    def test_scaffold_copies_detail_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._scaffold(instance)
            template = (
                instance
                / "memory"
                / "L2"
                / "accountabilities"
                / "<slug>.md.template"
            )
            self.assertTrue(template.exists(), f"missing {template}")
            body = template.read_text(encoding="utf-8")
            for section in (
                "## Scope",
                "## Out of scope",
                "## Outputs",
                "## Stakeholders",
                "## Cadence",
                "## Decision boundary",
                "## Adjacency notes",
                "## Self-check pre-action",
                "## Connections to existing constitution",
            ):
                self.assertIn(section, body, f"detail template missing {section}")

    def test_scaffold_idempotent_skips_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._scaffold(instance)

            manifest = instance / "memory" / "L1" / "accountabilities-manifest.md"
            readme = instance / "memory" / "L2" / "accountabilities" / "_README.md"
            manifest.write_text("CUSTOM CONTENT — do not overwrite\n", encoding="utf-8")
            readme.write_text("README CUSTOMIZED\n", encoding="utf-8")

            output = self._scaffold(instance)

            self.assertEqual(
                manifest.read_text(encoding="utf-8"),
                "CUSTOM CONTENT — do not overwrite\n",
            )
            self.assertEqual(
                readme.read_text(encoding="utf-8"),
                "README CUSTOMIZED\n",
            )
            self.assertIn("[skip]", output)

    def test_scaffold_prints_rules_snippet(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = self._scaffold(Path(tmp))
            self.assertIn("Accountability Principle", output)

    def test_scaffold_prints_paste_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = self._scaffold(Path(tmp))
            self.assertIn("RULES.md", output)

    def test_scaffold_creates_l2_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._scaffold(instance)
            l2_dir = instance / "memory" / "L2" / "accountabilities"
            self.assertTrue(l2_dir.is_dir(), f"missing dir {l2_dir}")


class ScaffoldCLAUDEPatchTests(unittest.TestCase):
    def test_scaffold_leaves_claude_md_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            claude_md = instance / "CLAUDE.md"
            original = "@memory/L1/IDENTITY.md\n@memory/L1/HOT.md\n"
            claude_md.write_text(original, encoding="utf-8")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                scaffold_accountabilities(instance)

            self.assertEqual(claude_md.read_text(encoding="utf-8"), original)
            self.assertNotIn("CLAUDE.md", buf.getvalue())


class ScaffoldEntitiesTests(unittest.TestCase):
    def _scaffold(self, instance: Path, *, migrate_people: bool = False) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scaffold_entities(instance, migrate_people=migrate_people)
        return buf.getvalue()

    def test_scaffold_copies_three_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._scaffold(instance)
            entities_dir = instance / "memory" / "L2" / "entities"
            for name in ("<slug>.md.template", "_README.md", "_categories.md"):
                self.assertTrue(
                    (entities_dir / name).exists(),
                    f"missing {entities_dir / name}",
                )

    def test_scaffold_idempotent_skips_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._scaffold(instance)
            readme = instance / "memory" / "L2" / "entities" / "_README.md"
            readme.write_text("CUSTOM\n", encoding="utf-8")
            output = self._scaffold(instance)
            self.assertEqual(readme.read_text(encoding="utf-8"), "CUSTOM\n")
            self.assertIn("[skip]", output)

    def test_scaffold_no_migration_leaves_people_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            people = instance / "memory" / "L2" / "people"
            people.mkdir(parents=True)
            (people / "alice.md").write_text(
                "---\nslug: people/alice\n---\nAlice\n", encoding="utf-8"
            )
            self._scaffold(instance, migrate_people=False)
            self.assertTrue((people / "alice.md").exists())

    def test_migration_moves_people_files_to_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            people = instance / "memory" / "L2" / "people"
            people.mkdir(parents=True)
            (people / "alice.md").write_text(
                "---\nslug: people/alice\n---\nAlice\n", encoding="utf-8"
            )
            (people / "bob.md").write_text(
                "---\nslug: people/bob\n---\nBob\n", encoding="utf-8"
            )
            self._scaffold(instance, migrate_people=True)

            self.assertFalse((people / "alice.md").exists())
            self.assertFalse((people / "bob.md").exists())

            archives = list((instance / "memory" / "L2" / "_archive").glob(
                "people-pre-*"
            ))
            self.assertEqual(len(archives), 1)
            archive_dir = archives[0]
            self.assertTrue((archive_dir / "alice.md").exists())
            self.assertTrue((archive_dir / "bob.md").exists())
            self.assertTrue((archive_dir / "_README.md").exists())
            readme = (archive_dir / "_README.md").read_text(encoding="utf-8")
            self.assertIn("alice.md", readme)
            self.assertIn("bob.md", readme)

    def test_migration_generates_stub_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            people = instance / "memory" / "L2" / "people"
            people.mkdir(parents=True)
            (people / "carol.md").write_text(
                "---\nslug: people/carol\n---\nCarol\n", encoding="utf-8"
            )
            self._scaffold(instance, migrate_people=True)

            stub = instance / "memory" / "L2" / "entities" / "carol.md"
            self.assertTrue(stub.exists(), f"missing {stub}")
            body = stub.read_text(encoding="utf-8")
            self.assertIn("slug: carol", body)
            self.assertIn("entity_category: unknown", body)
            self.assertIn("knowledge_state: inferred", body)
            self.assertIn("classification_confidence: low", body)

    def test_migration_is_one_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            people = instance / "memory" / "L2" / "people"
            people.mkdir(parents=True)
            (people / "dave.md").write_text(
                "---\nslug: people/dave\n---\nDave\n", encoding="utf-8"
            )
            self._scaffold(instance, migrate_people=True)

            # Re-create a new people file; second migration call must skip.
            people.mkdir(parents=True, exist_ok=True)
            (people / "eve.md").write_text(
                "---\nslug: people/eve\n---\nEve\n", encoding="utf-8"
            )
            output = self._scaffold(instance, migrate_people=True)
            self.assertIn("[skip] migration already ran", output)
            self.assertTrue((people / "eve.md").exists())  # untouched
            self.assertFalse(
                (instance / "memory" / "L2" / "entities" / "eve.md").exists()
            )


if __name__ == "__main__":
    unittest.main()
