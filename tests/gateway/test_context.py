"""Tests for lib/gateway/context.py preamble rendering.

Covers docs/specs/codex-main-brain-hardening.md §Phase 2 acceptance:

- Preamble includes CHATS.md when present.
- Preamble includes L2 memory command guidance.
- Preamble includes token-efficiency / caveman instructions.
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

    def test_preamble_includes_caveman_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp, {"IDENTITY.md": "id"})
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
