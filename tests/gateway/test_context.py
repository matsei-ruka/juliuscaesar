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

    def test_preamble_includes_manifest_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp, {"accountabilities-manifest.md": "# Manifest\n\nsome content"}
            )
            text = context.render_preamble(instance)
            self.assertIn("## accountabilities-manifest.md", text)
            self.assertIn("some content", text)

    def test_preamble_omits_manifest_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
            text = context.render_preamble(instance)
            self.assertNotIn("accountabilities-manifest.md", text)


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
