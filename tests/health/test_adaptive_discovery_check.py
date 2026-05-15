"""Tests for `health.adaptive_discovery_check.check_adaptive_discovery`.

Covers docs/specs/adaptive-discovery.md §Phase 5.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import config as gateway_config  # noqa: E402
from health.adaptive_discovery_check import check_adaptive_discovery  # noqa: E402


VALID_RULES = """# RULES

## §28 — ADAPTIVE DISCOVERY

This section governs how I handle inference.

- declared vs inferred knowledge
- the three cautions guide my reasoning
- the discovery protocol applies on first contact
- mutual self-disclosure with peer agents
"""

ENTITY_WITH_BASIS = """---
slug: jane-doe
entity_category: external_client
knowledge_state: declared
classification_confidence: high
confidence_basis: principal stated category
---
body
"""

ENTITY_WITHOUT_BASIS = """---
slug: john-roe
entity_category: unknown
knowledge_state: inferred
classification_confidence: low
confidence_basis:
---
body
"""


def _write_gateway(instance: Path, body: str) -> None:
    (instance / "ops").mkdir(exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(body, encoding="utf-8")
    gateway_config.clear_config_cache()


def _write_rules(instance: Path, body: str = VALID_RULES) -> None:
    target = instance / "memory" / "L1" / "RULES.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _write_entity(instance: Path, slug: str, body: str) -> None:
    target = instance / "memory" / "L2" / "entities" / f"{slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


class AdaptiveDiscoveryCheckTests(unittest.TestCase):
    def setUp(self):
        gateway_config.clear_config_cache()

    def tearDown(self):
        gateway_config.clear_config_cache()

    def test_disabled_returns_single_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway(
                instance, "adaptive_discovery:\n  enabled: false\n"
            )
            items = check_adaptive_discovery(instance)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].level, "info")
            self.assertIn("disabled", items[0].message.lower())

    def test_enabled_valid_rules_and_default_channel_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway(
                instance,
                "adaptive_discovery:\n  enabled: true\n"
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: telegram-primary\n",
            )
            _write_rules(instance)
            items = check_adaptive_discovery(instance)
            warns = [i for i in items if i.level == "warn"]
            self.assertEqual(warns, [], f"unexpected warnings: {warns}")
            self.assertTrue(
                any(
                    "adaptive-discovery section present" in i.message
                    for i in items
                )
            )
            self.assertTrue(
                any(
                    "escalation_channel resolves to authority_channel" in i.message
                    for i in items
                )
            )

    def test_rules_missing_section_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway(
                instance,
                "adaptive_discovery:\n  enabled: true\n"
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: telegram-primary\n",
            )
            _write_rules(instance, "# RULES\n\nnothing.")
            items = check_adaptive_discovery(instance)
            self.assertTrue(
                any(
                    "missing the Authority Awareness" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_rules_section_too_few_keywords_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway(
                instance,
                "adaptive_discovery:\n  enabled: true\n"
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: telegram-primary\n",
            )
            _write_rules(
                instance,
                "# RULES\n\n## Adaptive Discovery\n\nOnly declared mentioned.",
            )
            items = check_adaptive_discovery(instance)
            self.assertTrue(
                any(
                    "keyword phrases" in i.message and i.level == "warn"
                    for i in items
                )
            )

    def test_authority_alias_requires_accountabilities_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  high_stakes_escalation_channel: authority\n"
                "accountabilities:\n"
                "  enabled: false\n",
            )
            _write_rules(instance)
            items = check_adaptive_discovery(instance)
            self.assertTrue(
                any(
                    "requires accountabilities.enabled=true" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_authority_alias_rejects_authority_channel_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  high_stakes_escalation_channel: authority\n"
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: none\n",
            )
            _write_rules(instance)
            items = check_adaptive_discovery(instance)
            self.assertTrue(
                any(
                    "authority_channel=`none`" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_explicit_channel_must_be_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  high_stakes_escalation_channel: telegram\n",
            )
            _write_rules(instance)
            items = check_adaptive_discovery(instance)
            self.assertTrue(
                any(
                    "not configured in channels" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_explicit_channel_disabled_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway(
                instance,
                "channels:\n"
                "  telegram:\n"
                "    enabled: false\n"
                "    token_env: TELEGRAM_BOT_TOKEN\n"
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  high_stakes_escalation_channel: telegram\n",
            )
            _write_rules(instance)
            items = check_adaptive_discovery(instance)
            self.assertTrue(
                any(
                    "not configured" in i.message and i.level == "warn"
                    for i in items
                )
            )

    def test_explicit_channel_when_configured_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway(
                instance,
                "channels:\n"
                "  telegram:\n"
                "    enabled: true\n"
                "    token_env: TELEGRAM_BOT_TOKEN\n"
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  high_stakes_escalation_channel: telegram\n",
            )
            _write_rules(instance)
            items = check_adaptive_discovery(instance)
            self.assertTrue(
                any(
                    "escalation_channel `telegram` configured" in i.message
                    and i.level == "ok"
                    for i in items
                )
            )

    def test_confidence_basis_ratio_warn_when_below_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway(
                instance,
                "adaptive_discovery:\n  enabled: true\n"
                "entities:\n  enabled: true\n"
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: telegram-primary\n",
            )
            _write_rules(instance)
            # 1/5 = 20% have basis → below 80%.
            _write_entity(instance, "jane-doe", ENTITY_WITH_BASIS)
            for slug in ("a", "b", "c", "d"):
                _write_entity(
                    instance, slug, ENTITY_WITHOUT_BASIS.replace("john-roe", slug)
                )
            items = check_adaptive_discovery(instance)
            self.assertTrue(
                any(
                    "have confidence_basis" in i.message
                    and i.level == "warn"
                    for i in items
                )
            )

    def test_confidence_basis_ratio_ok_when_above_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway(
                instance,
                "adaptive_discovery:\n  enabled: true\n"
                "entities:\n  enabled: true\n"
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: telegram-primary\n",
            )
            _write_rules(instance)
            for slug in ("a", "b", "c", "d"):
                _write_entity(
                    instance,
                    slug,
                    ENTITY_WITH_BASIS.replace("jane-doe", slug),
                )
            items = check_adaptive_discovery(instance)
            self.assertTrue(
                any(
                    "have confidence_basis" in i.message
                    and i.level == "ok"
                    for i in items
                )
            )

    def test_confidence_basis_skipped_when_entities_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_gateway(
                instance,
                "adaptive_discovery:\n  enabled: true\n"
                "entities:\n  enabled: false\n"
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: telegram-primary\n",
            )
            _write_rules(instance)
            items = check_adaptive_discovery(instance)
            messages = " ".join(i.message for i in items)
            self.assertNotIn("confidence_basis", messages)


if __name__ == "__main__":
    unittest.main()
