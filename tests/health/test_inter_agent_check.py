"""Tests for `health.inter_agent_check.check_inter_agent`.

Covers docs/specs/inter-agent-protocol.md §Phase 5.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import config as gateway_config  # noqa: E402
from health.inter_agent_check import check_inter_agent  # noqa: E402


VALID_MAP = """---
slug: authority-map
title: Inter-Agent Authority Map
layer: L1
type: authority-map
state: active
version: 0.1.0
---

# Inter-Agent Authority Map

## Agents

| agent_id | display_name | role | human_authority | accountabilities_pointer | channel | instance_id |
|----------|--------------|------|-----------------|--------------------------|---------|-------------|
| rachel-zane | Rachel Zane | Executive strategist | luca-mattei | memory/L1/accountabilities-manifest.md | telegram:@rachel_zane_bot | rachel_zane |
| mario-leone | Mario Leone | COO | filippo-perta | TBD | telegram:@mario_leone_bot | mario_leone_coo |

## Self

self: rachel-zane

## Notes

Notes here.
"""

VALID_RULES = """# RULES

## §27 — INTER-AGENT PROTOCOL

This section governs how I behave with peer agents.

- authority symmetry
- perimeter respect
- mutual respect under pressure
- escalation transparency
- authority asymmetry preservation
"""


def _enable_config(instance: Path, *, require_self: bool = True) -> None:
    (instance / "ops").mkdir(exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(
        "inter_agent_protocol:\n"
        "  enabled: true\n"
        f"  require_self_declaration: {'true' if require_self else 'false'}\n",
        encoding="utf-8",
    )
    gateway_config.clear_config_cache()


def _disable_config(instance: Path) -> None:
    (instance / "ops").mkdir(exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(
        "inter_agent_protocol:\n  enabled: false\n",
        encoding="utf-8",
    )
    gateway_config.clear_config_cache()


def _write_map(instance: Path, body: str = VALID_MAP) -> None:
    target = instance / "memory" / "L1" / "authority-map.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _write_rules(instance: Path, body: str = VALID_RULES) -> None:
    target = instance / "memory" / "L1" / "RULES.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _write_manifest(instance: Path) -> None:
    target = instance / "memory" / "L1" / "accountabilities-manifest.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("placeholder manifest", encoding="utf-8")


class InterAgentCheckTests(unittest.TestCase):
    def setUp(self):
        gateway_config.clear_config_cache()

    def tearDown(self):
        gateway_config.clear_config_cache()

    def test_disabled_returns_single_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _disable_config(instance)
            items = check_inter_agent(instance)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].level, "info")
            self.assertIn("disabled", items[0].message.lower())

    def test_missing_map_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            items = check_inter_agent(instance)
            self.assertTrue(
                any(
                    i.level == "warn" and "authority-map missing" in i.message
                    for i in items
                )
            )

    def test_valid_map_and_rules_all_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_map(instance)
            _write_rules(instance)
            _write_manifest(instance)
            items = check_inter_agent(instance)
            warns = [i for i in items if i.level == "warn"]
            self.assertEqual(warns, [], f"unexpected warnings: {warns}")
            self.assertTrue(
                any("frontmatter valid" in i.message for i in items)
            )
            self.assertTrue(
                any("agent row(s) parsed" in i.message for i in items)
            )
            self.assertTrue(
                any("self=" in i.message and i.level == "ok" for i in items)
            )

    def test_invalid_frontmatter_slug_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            body = VALID_MAP.replace(
                "slug: authority-map", "slug: wrong-slug"
            )
            _write_map(instance, body)
            _write_rules(instance)
            items = check_inter_agent(instance)
            self.assertTrue(
                any(
                    "slug must be `authority-map`" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_missing_agents_table_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            body = """---
slug: authority-map
type: authority-map
state: active
---

# Map

## Self

self: rachel-zane
"""
            _write_map(instance, body)
            _write_rules(instance)
            items = check_inter_agent(instance)
            self.assertTrue(
                any(
                    "## Agents" in i.message and i.level == "warn"
                    for i in items
                )
            )

    def test_self_not_matching_row_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            body = VALID_MAP.replace(
                "self: rachel-zane", "self: missing-agent"
            )
            _write_map(instance, body)
            _write_rules(instance)
            items = check_inter_agent(instance)
            self.assertTrue(
                any(
                    "does not match any row" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_missing_self_with_require_self_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance, require_self=True)
            body = VALID_MAP.replace("self: rachel-zane", "")
            _write_map(instance, body)
            _write_rules(instance)
            items = check_inter_agent(instance)
            self.assertTrue(
                any(
                    "missing `self:" in i.message and i.level == "warn"
                    for i in items
                )
            )

    def test_missing_self_with_require_self_false_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance, require_self=False)
            body = VALID_MAP.replace("self: rachel-zane", "")
            _write_map(instance, body)
            _write_rules(instance)
            items = check_inter_agent(instance)
            self.assertTrue(
                any(
                    "missing `self:" in i.message and i.level == "info"
                    for i in items
                )
            )

    def test_unreachable_local_pointer_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_map(instance)  # pointer is memory/L1/accountabilities-manifest.md
            _write_rules(instance)
            # Do NOT write the manifest file.
            items = check_inter_agent(instance)
            self.assertTrue(
                any(
                    "does not resolve on disk" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_cross_instance_pointer_is_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            body = VALID_MAP.replace(
                "memory/L1/accountabilities-manifest.md",
                "/opt/peer/memory/L1/accountabilities-manifest.md",
            )
            _write_map(instance, body)
            _write_rules(instance)
            items = check_inter_agent(instance)
            self.assertTrue(
                any(
                    "cross-instance" in i.message and i.level == "info"
                    for i in items
                )
            )

    def test_tbd_pointer_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            body = VALID_MAP.replace(
                "memory/L1/accountabilities-manifest.md", "TBD"
            )
            _write_map(instance, body)
            _write_rules(instance)
            items = check_inter_agent(instance)
            # The mario-leone row also has TBD, so we should see no pointer
            # warnings or resolved-ok items mentioning rachel-zane's pointer.
            messages = " ".join(i.message for i in items)
            self.assertNotIn("does not resolve on disk", messages)

    def test_rules_missing_section_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_map(instance)
            _write_rules(instance, "# RULES\n\nnothing here")
            _write_manifest(instance)
            items = check_inter_agent(instance)
            self.assertTrue(
                any(
                    "missing the Inter-Agent Protocol" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_rules_section_too_few_keywords_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _enable_config(instance)
            _write_map(instance)
            _write_rules(
                instance,
                "# RULES\n\n## §27 — INTER-AGENT PROTOCOL\n\n"
                "Only authority symmetry mentioned.",
            )
            _write_manifest(instance)
            items = check_inter_agent(instance)
            self.assertTrue(
                any(
                    "principle keywords" in i.message and i.level == "warn"
                    for i in items
                )
            )


if __name__ == "__main__":
    unittest.main()
