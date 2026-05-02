"""Tests for `jc doctor` Codex readiness checks.

Covers docs/specs/codex-main-brain-hardening.md §Phase 6 acceptance:

- Codex auth file presence is reported.
- Instance `.codex/` is flagged when present but `CODEX_HOME` is not pointed
  at it (silently-ignored template).
- `default_brain: codex` with a write-capable sandbox produces a warning;
  read-only / no override is fine.
- Current model aliases are surfaced (sanity-check the public list).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import codex_diagnostics  # noqa: E402
from gateway.config import (  # noqa: E402
    BrainOverrideConfig,
    GatewayConfig,
)


class CodexAliasTests(unittest.TestCase):
    def test_aliases_include_current_codex_catalog(self):
        names = dict(codex_diagnostics.codex_aliases())
        # Phase 1 refresh — these must show up here too.
        self.assertEqual(names.get("gpt5"), "codex:gpt-5.4")
        self.assertEqual(names.get("mini"), "codex:gpt-5.4-mini")
        self.assertEqual(names.get("codex-coding"), "codex:gpt-5.3-codex")

    def test_alias_lines_are_two_columns(self):
        lines = codex_diagnostics.format_alias_lines([("gpt5", "codex:gpt-5.4")])
        self.assertEqual(len(lines), 1)
        self.assertIn("gpt5", lines[0])
        self.assertIn("->", lines[0])
        self.assertIn("codex:gpt-5.4", lines[0])


class AuthFindingTests(unittest.TestCase):
    def test_present_auth_file_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".codex").mkdir()
            (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
            f = codex_diagnostics.auth_finding(home)
            self.assertEqual(f.level, "ok")
            self.assertIn("auth.json", f.message)

    def test_missing_auth_file_is_info_not_fail(self):
        # Codex is optional — missing auth shouldn't fail the doctor run.
        with tempfile.TemporaryDirectory() as tmp:
            f = codex_diagnostics.auth_finding(Path(tmp))
            self.assertEqual(f.level, "info")


class InstanceCodexFindingTests(unittest.TestCase):
    def test_no_instance_codex_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                codex_diagnostics.instance_codex_finding(Path(tmp), codex_home_env=None)
            )

    def test_instance_codex_without_codex_home_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".codex").mkdir()
            f = codex_diagnostics.instance_codex_finding(instance, codex_home_env=None)
            self.assertIsNotNone(f)
            self.assertEqual(f.level, "warn")
            self.assertIn("CODEX_HOME", f.message)

    def test_instance_codex_with_unrelated_codex_home_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".codex").mkdir()
            other = Path(tmp) / "elsewhere"
            other.mkdir()
            f = codex_diagnostics.instance_codex_finding(
                instance, codex_home_env=str(other)
            )
            self.assertEqual(f.level, "warn")

    def test_instance_codex_active_when_codex_home_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            inst_codex = instance / ".codex"
            inst_codex.mkdir()
            f = codex_diagnostics.instance_codex_finding(
                instance, codex_home_env=str(inst_codex)
            )
            self.assertEqual(f.level, "ok")
            self.assertIn("active", f.message)


class SandboxFindingTests(unittest.TestCase):
    def _cfg(self, *, default_brain: str = "codex", override: BrainOverrideConfig | None = None) -> GatewayConfig:
        brains: dict[str, BrainOverrideConfig] = {}
        if override is not None:
            brains["codex"] = override
        return GatewayConfig(default_brain=default_brain, brains=brains)

    def test_no_finding_when_default_brain_is_not_codex(self):
        self.assertIsNone(codex_diagnostics.sandbox_finding(self._cfg(default_brain="claude")))

    def test_default_codex_with_no_override_is_ok_read_only(self):
        f = codex_diagnostics.sandbox_finding(self._cfg())
        self.assertIsNotNone(f)
        self.assertEqual(f.level, "ok")
        self.assertIn("read-only", f.message)

    def test_explicit_read_only_is_ok(self):
        f = codex_diagnostics.sandbox_finding(
            self._cfg(override=BrainOverrideConfig(sandbox="read-only"))
        )
        self.assertEqual(f.level, "ok")

    def test_workspace_write_warns(self):
        f = codex_diagnostics.sandbox_finding(
            self._cfg(override=BrainOverrideConfig(sandbox="workspace-write"))
        )
        self.assertEqual(f.level, "warn")
        self.assertIn("workspace-write", f.message)

    def test_yolo_warns(self):
        f = codex_diagnostics.sandbox_finding(
            self._cfg(override=BrainOverrideConfig(yolo=True))
        )
        self.assertEqual(f.level, "warn")
        self.assertIn("yolo", f.message)


if __name__ == "__main__":
    unittest.main()
