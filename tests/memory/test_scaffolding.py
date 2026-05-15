"""Tests for `memory.scaffolding.scaffold_accountabilities`.

Covers docs/specs/accountabilities.md §Phase 3:
- Manifest + L2 README are copied into the instance.
- Idempotent: existing files are skipped (content preserved).
- The constitutional RULES snippet is printed (not written).
- A paste instruction is printed pointing the operator at RULES.md.
- The L2 accountabilities directory is created.
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


if __name__ == "__main__":
    unittest.main()
