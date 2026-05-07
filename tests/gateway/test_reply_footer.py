from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.config import ConfigError, ReplyFooterConfig, load_config, render_default_config  # noqa: E402
from gateway.reply_footer import render_footer  # noqa: E402


class ReplyFooterRenderTest(unittest.TestCase):
    def test_all_segments_present(self) -> None:
        footer = render_footer(
            ReplyFooterConfig(enabled=True),
            brain="claude",
            model="sonnet-4-6",
            session_id="7f3a4b21c0ffee",
            elapsed_seconds=4.2,
        )
        self.assertEqual(footer, "⚙️ claude:sonnet-4-6 · sess 7f3a4b21… · 4.2s")

    def test_model_none_uses_bare_brain(self) -> None:
        footer = render_footer(
            ReplyFooterConfig(enabled=True),
            brain="claude",
            model=None,
            session_id="sess-123",
            elapsed_seconds=4.2,
        )
        self.assertEqual(footer, "⚙️ claude · sess sess-123 · 4.2s")

    def test_session_none_can_be_shown_or_skipped(self) -> None:
        shown = render_footer(
            ReplyFooterConfig(enabled=True),
            brain="claude",
            model=None,
            session_id=None,
            elapsed_seconds=1,
        )
        skipped = render_footer(
            ReplyFooterConfig(enabled=True, show_session=False),
            brain="claude",
            model=None,
            session_id=None,
            elapsed_seconds=1,
        )
        self.assertEqual(shown, "⚙️ claude · sess none · 1.0s")
        self.assertEqual(skipped, "⚙️ claude · 1.0s")

    def test_elapsed_over_minute_uses_clock_format(self) -> None:
        footer = render_footer(
            ReplyFooterConfig(enabled=True, show_model=False, show_session=False),
            brain="claude",
            model=None,
            session_id=None,
            elapsed_seconds=83.4,
        )
        self.assertEqual(footer, "⚙️ 01:23")

    def test_disabled_or_empty_returns_none(self) -> None:
        self.assertIsNone(
            render_footer(
                ReplyFooterConfig(),
                brain="claude",
                model=None,
                session_id=None,
                elapsed_seconds=1,
            )
        )
        self.assertIsNone(
            render_footer(
                ReplyFooterConfig(
                    enabled=True,
                    show_model=False,
                    show_session=False,
                    show_elapsed=False,
                ),
                brain="claude",
                model=None,
                session_id=None,
                elapsed_seconds=1,
            )
        )


class ReplyFooterConfigTest(unittest.TestCase):
    def _instance_with_config(self, config_text: str) -> Path:
        root = Path(tempfile.mkdtemp(prefix="jc-reply-footer-config-"))
        (root / ".jc").write_text("", encoding="utf-8")
        (root / "ops").mkdir()
        (root / "ops" / "gateway.yaml").write_text(config_text, encoding="utf-8")
        return root

    def test_loads_nested_reply_footer_config(self) -> None:
        cfg = load_config(
            self._instance_with_config(
                render_default_config(default_brain="claude")
                + """
reply_footer:
  enabled: true
  emoji: "dbg"
  show_model: false
  show_session: true
  show_elapsed: false
  session_chars: 12
  separator: " | "
"""
            )
        )
        self.assertTrue(cfg.reply_footer.enabled)
        self.assertEqual(cfg.reply_footer.emoji, "dbg")
        self.assertFalse(cfg.reply_footer.show_model)
        self.assertTrue(cfg.reply_footer.show_session)
        self.assertFalse(cfg.reply_footer.show_elapsed)
        self.assertEqual(cfg.reply_footer.session_chars, 12)
        self.assertEqual(cfg.reply_footer.separator, " | ")

    def test_rejects_bad_reply_footer_config(self) -> None:
        cases = [
            ("reply_footer: yes\n", "reply_footer: must be a mapping"),
            ("reply_footer:\n  foo: true\n", "reply_footer.foo: unknown field"),
            ("reply_footer:\n  enabled: nope\n", "reply_footer.enabled: must be boolean"),
            ("reply_footer:\n  session_chars: 2\n", "reply_footer.session_chars"),
            ("reply_footer:\n  emoji: ''\n", "reply_footer.emoji"),
            ("reply_footer:\n  separator: ''\n", "reply_footer.separator"),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                with self.assertRaisesRegex(ConfigError, message):
                    load_config(
                        self._instance_with_config(
                            render_default_config(default_brain="claude") + body
                        )
                    )


if __name__ == "__main__":
    unittest.main()
