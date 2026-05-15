"""Tests for lib/gateway/context.py preamble rendering.

Covers docs/specs/codex-main-brain-hardening.md §Phase 2 acceptance:

- Preamble includes CHATS.md when present.
- Preamble includes L2 memory command guidance.
- Preamble includes token-efficiency / caveman instructions only when STYLE opts in.
- Updating any L1 file (including CHATS.md) invalidates the cache.
- Render is resilient when CHATS.md (or any L1 file) is missing.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import context  # noqa: E402
from gateway import config as gateway_config  # noqa: E402


def _make_instance(tmp: str, files: dict[str, str] | None = None) -> Path:
    instance = Path(tmp)
    l1 = instance / "memory" / "L1"
    l1.mkdir(parents=True)
    for name, body in (files or {}).items():
        (l1 / name).write_text(body, encoding="utf-8")
    return instance


class PreambleContentTests(unittest.TestCase):
    def setUp(self):
        context.clear_cache()
        gateway_config.clear_config_cache()

    def tearDown(self):
        context.clear_cache()
        gateway_config.clear_config_cache()

    def _write_gateway_yaml(self, instance: Path, body: str) -> None:
        ops = instance / "ops"
        ops.mkdir(parents=True, exist_ok=True)
        (ops / "gateway.yaml").write_text(body, encoding="utf-8")
        gateway_config.clear_config_cache()

    def test_preamble_includes_chats_md_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp,
                {
                    "IDENTITY.md": "id body",
                    "CHATS.md": "## Known Telegram chats\n- 12345 | private | Luca",
                },
            )
            text = context.render_preamble(instance)
            self.assertIn("## CHATS.md", text)
            self.assertIn("Known Telegram chats", text)
            self.assertIn("Luca", text)

    def test_preamble_includes_l2_memory_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            text = context.render_preamble(instance)
            self.assertIn("jc memory search", text)
            self.assertIn("jc memory read", text)
            self.assertIn("jc transcripts", text)

    def test_preamble_omits_caveman_instructions_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            text = context.render_preamble(instance)
            self.assertNotIn("caveman", text.lower())
            self.assertNotIn("/caveman", text)

    def test_preamble_includes_caveman_instructions_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp,
                {
                    "IDENTITY.md": "id",
                    "STYLE.md": "# Voice anchor\n\n> voice\n\n## Caveman\n\ncaveman: enabled\n",
                },
            )
            text = context.render_preamble(instance)
            self.assertIn("caveman", text.lower())
            self.assertIn("/caveman", text)

    def test_preamble_role_marks_chat_brain_not_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            text = context.render_preamble(instance)
            self.assertIn("gateway chat brain", text)
            self.assertIn("not as an autonomous worker", text)

    def test_preamble_resilient_when_chats_md_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id only"})
            text = context.render_preamble(instance)
            self.assertIn("id only", text)
            self.assertNotIn("## CHATS.md", text)

    def test_preamble_handles_empty_l1(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {})
            text = context.render_preamble(instance)
            self.assertIn("(No L1 memory files found.)", text)
            self.assertIn("jc memory", text)

    def test_preamble_includes_manifest_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp, {"accountabilities-manifest.md": "# Manifest\n\nsome content"}
            )
            self._write_gateway_yaml(
                instance,
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: telegram-primary\n",
            )
            text = context.render_preamble(instance)
            self.assertIn("## accountabilities-manifest.md", text)
            self.assertIn("some content", text)

    def test_preamble_omits_manifest_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp, {"accountabilities-manifest.md": "# Manifest\n\nsome content"}
            )
            self._write_gateway_yaml(
                instance,
                "accountabilities:\n"
                "  enabled: false\n",
            )
            text = context.render_preamble(instance)
            self.assertNotIn("accountabilities-manifest.md", text)
            self.assertNotIn("some content", text)

    def test_preamble_omits_manifest_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            text = context.render_preamble(instance)
            self.assertNotIn("accountabilities-manifest.md", text)

    def test_gateway_config_toggle_invalidates_preamble_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp,
                {
                    "IDENTITY.md": "id",
                    "accountabilities-manifest.md": "# Manifest\n\nsome content",
                },
            )
            self._write_gateway_yaml(
                instance,
                "accountabilities:\n"
                "  enabled: false\n",
            )
            first = context.render_preamble(instance)
            self.assertNotIn("accountabilities-manifest.md", first)

            config_path = instance / "ops" / "gateway.yaml"
            config_path.write_text(
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: telegram-primary\n",
                encoding="utf-8",
            )
            future = time.time() + 5
            os.utime(config_path, (future, future))
            gateway_config.clear_config_cache()

            second = context.render_preamble(instance)
            self.assertIn("accountabilities-manifest.md", second)
            self.assertIn("some content", second)


class AuthorityBlockTests(unittest.TestCase):
    def setUp(self):
        from gateway import config as gateway_config

        gateway_config.clear_config_cache()

    def tearDown(self):
        from gateway import config as gateway_config

        gateway_config.clear_config_cache()

    def _write_gateway_yaml(self, instance: Path, body: str) -> None:
        ops = instance / "ops"
        ops.mkdir(parents=True, exist_ok=True)
        (ops / "gateway.yaml").write_text(body, encoding="utf-8")
        gateway_config.clear_config_cache()

    def test_authority_block_empty_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._write_gateway_yaml(
                instance, "accountabilities:\n  enabled: false\n"
            )
            self.assertEqual(context.render_authority_block(instance), "")

    def test_authority_block_empty_when_config_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(context.render_authority_block(Path(tmp)), "")

    def test_authority_block_renders_token_and_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._write_gateway_yaml(
                instance,
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: telegram-primary\n"
                "  enactment_token: MAKE IT SO\n",
            )
            block = context.render_authority_block(instance)
            self.assertIn("authority_channel: `telegram-primary`", block)
            self.assertIn("enactment_token: `MAKE IT SO`", block)
            self.assertIn("Casual agreement", block)

    def test_authority_block_telegram_primary_includes_primary_chat_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._write_gateway_yaml(
                instance,
                "channels:\n"
                "  telegram:\n"
                "    chat_ids: [\"111\", \"222\"]\n"
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: telegram-primary\n"
                "  enactment_token: OK enact\n",
            )
            block = context.render_authority_block(instance)
            self.assertIn("telegram_primary_chat_id: `111`", block)
            self.assertIn("metadata must match `telegram_primary_chat_id`", block)

    def test_authority_block_email_includes_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._write_gateway_yaml(
                instance,
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: email\n"
                "  enactment_token: OK enact\n"
                "  authority_email_sender: ceo@example.com\n",
            )
            block = context.render_authority_block(instance)
            self.assertIn("authority_email_sender: `ceo@example.com`", block)

    def test_authority_block_channel_none_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self._write_gateway_yaml(
                instance,
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: none\n"
                "  enactment_token: OK enact\n",
            )
            block = context.render_authority_block(instance)
            self.assertIn("authority_channel: `none`", block)
            self.assertIn("refuse every enactment", block)


class EntitiesBlockTests(unittest.TestCase):
    def setUp(self):
        context.clear_cache()
        gateway_config.clear_config_cache()

    def tearDown(self):
        context.clear_cache()
        gateway_config.clear_config_cache()

    def _write_gateway_yaml(self, instance: Path, body: str) -> None:
        ops = instance / "ops"
        ops.mkdir(parents=True, exist_ok=True)
        (ops / "gateway.yaml").write_text(body, encoding="utf-8")
        gateway_config.clear_config_cache()

    def test_entities_block_empty_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(instance, "entities:\n  enabled: false\n")
            self.assertEqual(context.render_entities_block(instance), "")

    def test_entities_block_empty_when_config_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(context.render_entities_block(Path(tmp)), "")

    def test_entities_block_renders_pointer_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(instance, "entities:\n  enabled: true\n")
            block = context.render_entities_block(instance)
            self.assertEqual(
                block,
                "Entities directory: memory/L2/entities/ "
                "(six categories, see _categories.md).",
            )

    def test_preamble_includes_entities_pointer_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(instance, "entities:\n  enabled: true\n")
            text = context.render_preamble(instance)
            self.assertIn("Entities directory: memory/L2/entities/", text)

    def test_preamble_omits_entities_pointer_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(instance, "entities:\n  enabled: false\n")
            text = context.render_preamble(instance)
            self.assertNotIn("Entities directory:", text)


class AuthorityMapBlockTests(unittest.TestCase):
    def setUp(self):
        context.clear_cache()
        gateway_config.clear_config_cache()

    def tearDown(self):
        context.clear_cache()
        gateway_config.clear_config_cache()

    def _write_gateway_yaml(self, instance: Path, body: str) -> None:
        ops = instance / "ops"
        ops.mkdir(parents=True, exist_ok=True)
        (ops / "gateway.yaml").write_text(body, encoding="utf-8")
        gateway_config.clear_config_cache()

    def _write_authority_map(self, instance: Path, body: str) -> None:
        l1 = instance / "memory" / "L1"
        l1.mkdir(parents=True, exist_ok=True)
        (l1 / "authority-map.md").write_text(body, encoding="utf-8")

    def test_authority_map_block_empty_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance, "inter_agent_protocol:\n  enabled: false\n"
            )
            self.assertEqual(context.render_authority_map_block(instance), "")

    def test_authority_map_block_empty_when_config_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(context.render_authority_map_block(Path(tmp)), "")

    def test_authority_map_block_empty_when_file_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance, "inter_agent_protocol:\n  enabled: true\n"
            )
            self.assertEqual(context.render_authority_map_block(instance), "")

    def test_authority_map_block_renders_content_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance, "inter_agent_protocol:\n  enabled: true\n"
            )
            self._write_authority_map(
                instance,
                "---\nslug: authority-map\n---\n\n## Agents\n\n| agent_id | channel |\n",
            )
            block = context.render_authority_map_block(instance)
            self.assertTrue(block.startswith("# Inter-agent authority map\n"))
            self.assertIn("## Agents", block)

    def test_preamble_includes_authority_map_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance, "inter_agent_protocol:\n  enabled: true\n"
            )
            self._write_authority_map(instance, "## Agents\n\n| agent_id | channel |\n")
            text = context.render_preamble(instance)
            self.assertIn("# Inter-agent authority map", text)

    def test_preamble_omits_authority_map_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance, "inter_agent_protocol:\n  enabled: false\n"
            )
            self._write_authority_map(instance, "## Agents\n\n| agent_id | channel |\n")
            text = context.render_preamble(instance)
            self.assertNotIn("Inter-agent authority map", text)

    def test_authority_map_file_change_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance, "inter_agent_protocol:\n  enabled: true\n"
            )
            first = context.render_preamble(instance)
            self.assertNotIn("Inter-agent authority map", first)

            self._write_authority_map(instance, "## Agents\n\n| agent_id | channel |\n")
            map_path = instance / "memory" / "L1" / "authority-map.md"
            future = time.time() + 5
            os.utime(map_path, (future, future))

            second = context.render_preamble(instance)
            self.assertIn("Inter-agent authority map", second)


class AdaptiveDiscoveryBlockTests(unittest.TestCase):
    def setUp(self):
        context.clear_cache()
        gateway_config.clear_config_cache()

    def tearDown(self):
        context.clear_cache()
        gateway_config.clear_config_cache()

    def _write_gateway_yaml(self, instance: Path, body: str) -> None:
        ops = instance / "ops"
        ops.mkdir(parents=True, exist_ok=True)
        (ops / "gateway.yaml").write_text(body, encoding="utf-8")
        gateway_config.clear_config_cache()

    def test_adaptive_block_empty_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance, "adaptive_discovery:\n  enabled: false\n"
            )
            self.assertEqual(context.render_adaptive_discovery_block(instance), "")

    def test_adaptive_block_empty_when_config_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                context.render_adaptive_discovery_block(Path(tmp)), ""
            )

    def test_adaptive_block_uses_authority_channel_when_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  high_stakes_escalation_channel: authority\n"
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: telegram-primary\n",
            )
            block = context.render_adaptive_discovery_block(instance)
            self.assertIn("escalate via telegram-primary", block)
            self.assertTrue(block.startswith("# Adaptive discovery — live reminder\n"))

    def test_adaptive_block_uses_explicit_channel_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  high_stakes_escalation_channel: telegram\n",
            )
            block = context.render_adaptive_discovery_block(instance)
            self.assertIn("escalate via telegram", block)

    def test_adaptive_block_fallback_when_accountabilities_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  high_stakes_escalation_channel: authority\n"
                "accountabilities:\n"
                "  enabled: false\n",
            )
            block = context.render_adaptive_discovery_block(instance)
            self.assertIn("escalate via the human authority", block)

    def test_adaptive_block_fallback_when_authority_channel_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n"
                "  high_stakes_escalation_channel: authority\n"
                "accountabilities:\n"
                "  enabled: true\n"
                "  authority_channel: none\n",
            )
            block = context.render_adaptive_discovery_block(instance)
            self.assertIn("escalate via the human authority", block)

    def test_preamble_includes_adaptive_block_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance,
                "adaptive_discovery:\n"
                "  enabled: true\n",
            )
            text = context.render_preamble(instance)
            self.assertIn("Adaptive discovery — live reminder", text)
            self.assertIn("Knowledge states:", text)
            self.assertIn("Unknown default:", text)

    def test_preamble_omits_adaptive_block_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            self._write_gateway_yaml(
                instance, "adaptive_discovery:\n  enabled: false\n"
            )
            text = context.render_preamble(instance)
            self.assertNotIn("Adaptive discovery", text)


class CacheInvalidationTests(unittest.TestCase):
    def setUp(self):
        context.clear_cache()

    def test_updating_chats_md_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp,
                {
                    "IDENTITY.md": "id",
                    "CHATS.md": "version-one",
                },
            )
            first = context.render_preamble(instance)
            self.assertIn("version-one", first)

            # Bump mtime forward so the fingerprint changes even on fast
            # filesystems where rewrites can land within the same mtime tick.
            chats_path = instance / "memory" / "L1" / "CHATS.md"
            chats_path.write_text("version-two", encoding="utf-8")
            future = time.time() + 5
            os.utime(chats_path, (future, future))

            second = context.render_preamble(instance)
            self.assertIn("version-two", second)
            self.assertNotIn("version-one", second)

    def test_cache_hit_when_files_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "stable"})
            first = context.render_preamble(instance)
            second = context.render_preamble(instance)
            self.assertIs(first, second)  # cache returns same string object


if __name__ == "__main__":
    unittest.main()
