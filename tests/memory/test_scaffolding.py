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

from memory.scaffolding import scaffold_accountabilities  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
