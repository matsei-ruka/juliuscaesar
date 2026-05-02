"""Tests for the brain capability matrix.

Covers docs/specs/codex-main-brain-hardening.md §"Images and multimodal
input" (capability matrix v1). The matrix replaces the hardcoded
`brain not in ("claude", "gemini")` check that previously force-routed
Codex events away from Codex even though Codex CLI supports `--image`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import capabilities  # noqa: E402


class CapabilityMatrixTests(unittest.TestCase):
    def test_codex_cli_supports_images(self):
        self.assertTrue(capabilities.supports_images("codex"))

    def test_codex_api_does_not_support_images(self):
        # codex_api uses the Responses API; image-part payloads are not yet
        # implemented or tested per spec.
        self.assertFalse(capabilities.supports_images("codex_api"))

    def test_claude_and_gemini_support_images(self):
        self.assertTrue(capabilities.supports_images("claude"))
        self.assertTrue(capabilities.supports_images("gemini"))

    def test_text_only_brains_do_not_support_images(self):
        self.assertFalse(capabilities.supports_images("opencode"))
        self.assertFalse(capabilities.supports_images("aider"))

    def test_unknown_brain_defaults_to_no_images(self):
        # Unknown brains fail closed: no image support assumed.
        self.assertFalse(capabilities.supports_images("mystery"))

    def test_for_brain_returns_dataclass(self):
        caps = capabilities.for_brain("codex")
        self.assertTrue(caps.text)
        self.assertTrue(caps.images)
        self.assertTrue(caps.tools)
        self.assertTrue(caps.file_edits)


if __name__ == "__main__":
    unittest.main()
