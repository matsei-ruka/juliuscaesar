"""Regression coverage for instance-local .env boundaries."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import config as gateway_config  # noqa: E402
from gateway.config import (  # noqa: E402
    ConfigError,
    clear_env_cache,
    env_value,
    load_config,
    merge_instance_env,
)


class ConfigEnvBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_env_cache()

    def tearDown(self) -> None:
        clear_env_cache()

    def test_env_value_prefers_instance_env_over_process_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=instance-token\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "process-token"},
                clear=False,
            ):
                self.assertEqual(
                    env_value(instance, "TELEGRAM_BOT_TOKEN"),
                    "instance-token",
                )

    def test_env_value_falls_back_to_process_env_when_key_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text("OTHER=value\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "process-token"},
                clear=False,
            ):
                self.assertEqual(
                    env_value(instance, "TELEGRAM_BOT_TOKEN"),
                    "process-token",
                )

    def test_two_instances_under_same_user_resolve_their_own_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alpha = root / "alpha"
            beta = root / "beta"
            alpha.mkdir()
            beta.mkdir()
            (alpha / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=alpha-token\n",
                encoding="utf-8",
            )
            (beta / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=beta-token\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "ambient-token"},
                clear=False,
            ):
                self.assertEqual(env_value(alpha, "TELEGRAM_BOT_TOKEN"), "alpha-token")
                self.assertEqual(env_value(beta, "TELEGRAM_BOT_TOKEN"), "beta-token")

    def test_env_value_ignores_reserved_instance_runtime_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text(
                "PATH=/evil\n"
                "JC_EVENT_SOURCE=wrong\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"PATH": "/usr/bin:/bin", "JC_EVENT_SOURCE": "cron"},
                clear=False,
            ):
                self.assertEqual(env_value(instance, "PATH"), "/usr/bin:/bin")
                self.assertEqual(env_value(instance, "JC_EVENT_SOURCE"), "cron")

    def test_merge_instance_env_blocks_runtime_control_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=instance-token\n"
                "PATH=/evil\n"
                "RUNTIME_MODE=legacy-claude\n"
                "JC_EVENT_SOURCE=wrong\n",
                encoding="utf-8",
            )
            merged = merge_instance_env(
                instance,
                {
                    "TELEGRAM_BOT_TOKEN": "process-token",
                    "PATH": "/usr/bin:/bin",
                    "JC_EVENT_SOURCE": "cron",
                },
            )

            self.assertEqual(merged["TELEGRAM_BOT_TOKEN"], "instance-token")
            self.assertEqual(merged["PATH"], "/usr/bin:/bin")
            self.assertEqual(merged["JC_EVENT_SOURCE"], "cron")
            self.assertNotIn("RUNTIME_MODE", merged)


def _write_yaml(instance: Path, body: str) -> None:
    (instance / "ops").mkdir(exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(body)
    gateway_config.clear_config_cache()


class AccountabilitiesSchemaTests(unittest.TestCase):
    """Covers docs/specs/accountabilities.md §Phase 2 — Config schema."""

    def test_accountabilities_block_missing_defaults_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "default_brain: claude\n")
            cfg = load_config(instance)
            self.assertFalse(cfg.accountabilities.enabled)
            self.assertEqual(cfg.accountabilities.authority_channel, "telegram-primary")
            self.assertEqual(cfg.accountabilities.enactment_token, "OK enact")
            self.assertEqual(cfg.accountabilities.authority_email_sender, "")

    def test_accountabilities_disabled_skips_other_field_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "accountabilities:\n"
                "  enabled: false\n"
                "  authority_channel: not-a-real-channel\n",
            )
            cfg = load_config(instance)
            self.assertFalse(cfg.accountabilities.enabled)

    def test_accountabilities_enabled_telegram_primary_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: telegram-primary\n",
            )
            cfg = load_config(instance)
            self.assertTrue(cfg.accountabilities.enabled)
            self.assertEqual(cfg.accountabilities.authority_channel, "telegram-primary")


class SessionLifecycleSchemaTests(unittest.TestCase):
    def test_rejects_not_yet_supported_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "session_lifecycle:\n"
                "  enabled: true\n"
                "  idle:\n"
                "    after_seconds: 30\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("session_lifecycle.idle: field idle not yet supported", str(ctx.exception))

    def test_accountabilities_enabled_email_without_sender_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: email\n"
                "  authority_email_sender: ''\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("authority_email_sender", str(ctx.exception))

    def test_accountabilities_enabled_email_with_sender_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: email\n"
                "  authority_email_sender: boss@example.com\n",
            )
            cfg = load_config(instance)
            self.assertEqual(cfg.accountabilities.authority_channel, "email")
            self.assertEqual(
                cfg.accountabilities.authority_email_sender, "boss@example.com"
            )

    def test_accountabilities_enabled_none_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: none\n",
            )
            cfg = load_config(instance)
            self.assertTrue(cfg.accountabilities.enabled)
            self.assertEqual(cfg.accountabilities.authority_channel, "none")

    def test_accountabilities_enabled_invalid_channel_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: invalid-value\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("authority_channel", str(ctx.exception))


class EntitiesSchemaTests(unittest.TestCase):
    """Covers docs/specs/relational-awareness-layer.md §Phase 2 — Config schema."""

    def test_entities_block_missing_defaults_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "default_brain: claude\n")
            cfg = load_config(instance)
            self.assertFalse(cfg.entities.enabled)
            self.assertFalse(cfg.entities.migrate_legacy_people)

    def test_entities_enabled_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "entities:\n"
                "  enabled: true\n"
                "  migrate_legacy_people: true\n",
            )
            cfg = load_config(instance)
            self.assertTrue(cfg.entities.enabled)
            self.assertTrue(cfg.entities.migrate_legacy_people)

    def test_entities_disabled_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "entities:\n"
                "  enabled: false\n",
            )
            cfg = load_config(instance)
            self.assertFalse(cfg.entities.enabled)
            self.assertFalse(cfg.entities.migrate_legacy_people)

    def test_entities_unknown_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "entities:\n"
                "  enabled: true\n"
                "  bogus_field: value\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("entities.bogus_field", str(ctx.exception))

    def test_entities_non_mapping_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "entities: not-a-mapping\n")
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("entities", str(ctx.exception))

    def test_entities_non_boolean_enabled_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "entities:\n"
                "  enabled: maybe\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("entities.enabled", str(ctx.exception))


class InterAgentProtocolSchemaTests(unittest.TestCase):
    """Covers docs/specs/inter-agent-protocol.md §Phase 2 — Config schema."""

    def test_inter_agent_block_missing_defaults_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "default_brain: claude\n")
            cfg = load_config(instance)
            self.assertFalse(cfg.inter_agent_protocol.enabled)
            self.assertEqual(
                cfg.inter_agent_protocol.authority_map_path,
                "memory/L1/authority-map.md",
            )
            self.assertTrue(cfg.inter_agent_protocol.require_self_declaration)

    def test_inter_agent_enabled_full_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "inter_agent_protocol:\n"
                "  enabled: true\n"
                "  authority_map_path: memory/L1/custom-map.md\n"
                "  require_self_declaration: false\n",
            )
            cfg = load_config(instance)
            self.assertTrue(cfg.inter_agent_protocol.enabled)
            self.assertEqual(
                cfg.inter_agent_protocol.authority_map_path,
                "memory/L1/custom-map.md",
            )
            self.assertFalse(cfg.inter_agent_protocol.require_self_declaration)

    def test_inter_agent_unknown_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "inter_agent_protocol:\n"
                "  enabled: true\n"
                "  bogus: value\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("inter_agent_protocol.bogus", str(ctx.exception))

    def test_inter_agent_non_mapping_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "inter_agent_protocol: scalar\n")
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("inter_agent_protocol", str(ctx.exception))

    def test_inter_agent_non_boolean_enabled_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "inter_agent_protocol:\n"
                "  enabled: not-bool\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("inter_agent_protocol.enabled", str(ctx.exception))

    def test_inter_agent_non_boolean_self_declaration_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "inter_agent_protocol:\n"
                "  enabled: true\n"
                "  require_self_declaration: nope\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn(
                "inter_agent_protocol.require_self_declaration",
                str(ctx.exception),
            )

    def test_inter_agent_empty_authority_map_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "inter_agent_protocol:\n"
                "  enabled: true\n"
                "  authority_map_path: ''\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn(
                "inter_agent_protocol.authority_map_path",
                str(ctx.exception),
            )


class AdaptiveDiscoverySchemaTests(unittest.TestCase):
    """Covers docs/specs/adaptive-discovery.md §Phase 2 — Config schema."""

    def test_adaptive_discovery_block_missing_defaults_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "default_brain: claude\n")
            cfg = load_config(instance)
            self.assertFalse(cfg.adaptive_discovery.enabled)
            self.assertEqual(
                cfg.adaptive_discovery.default_unknown_posture, "conservative"
            )
            self.assertEqual(
                cfg.adaptive_discovery.high_stakes_escalation_channel, "authority"
            )

    def test_adaptive_discovery_enabled_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  default_unknown_posture: conservative\n"
                "  high_stakes_escalation_channel: authority\n",
            )
            cfg = load_config(instance)
            self.assertTrue(cfg.adaptive_discovery.enabled)
            self.assertEqual(
                cfg.adaptive_discovery.default_unknown_posture, "conservative"
            )
            self.assertEqual(
                cfg.adaptive_discovery.high_stakes_escalation_channel, "authority"
            )

    def test_adaptive_discovery_explicit_channel_slug_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  high_stakes_escalation_channel: telegram\n",
            )
            cfg = load_config(instance)
            self.assertEqual(
                cfg.adaptive_discovery.high_stakes_escalation_channel, "telegram"
            )

    def test_adaptive_discovery_invalid_posture_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  default_unknown_posture: aggressive\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn(
                "adaptive_discovery.default_unknown_posture",
                str(ctx.exception),
            )

    def test_adaptive_discovery_invalid_channel_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  high_stakes_escalation_channel: pigeon\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn(
                "adaptive_discovery.high_stakes_escalation_channel",
                str(ctx.exception),
            )

    def test_adaptive_discovery_unknown_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  forbid_high_inferred: true\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn(
                "adaptive_discovery.forbid_high_inferred",
                str(ctx.exception),
            )

    def test_adaptive_discovery_non_mapping_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(instance, "adaptive_discovery: scalar\n")
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("adaptive_discovery", str(ctx.exception))

    def test_adaptive_discovery_non_boolean_enabled_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            _write_yaml(
                instance,
                "adaptive_discovery:\n"
                "  enabled: yesplease\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(instance)
            self.assertIn("adaptive_discovery.enabled", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
