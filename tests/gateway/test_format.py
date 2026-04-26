"""Tests for the outbound MarkdownV2 escaper."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.format import to_markdown_v2  # noqa: E402


class PlainTextEscapeTests(unittest.TestCase):
    def test_empty_passthrough(self):
        self.assertEqual(to_markdown_v2(""), "")

    def test_no_reserved_chars_unchanged(self):
        self.assertEqual(to_markdown_v2("hello luca"), "hello luca")

    def test_period_escaped(self):
        self.assertEqual(to_markdown_v2("Hello, world."), "Hello, world\\.")

    def test_multiple_reserved_chars(self):
        # `:` is NOT reserved; `.` and `-` are.
        self.assertEqual(
            to_markdown_v2("Now: 13:45 - all good."),
            "Now: 13:45 \\- all good\\.",
        )

    def test_literal_backslash_escaped(self):
        self.assertEqual(to_markdown_v2("path\\to\\file"), "path\\\\to\\\\file")

    def test_parens_escaped(self):
        self.assertEqual(to_markdown_v2("call now (urgent)"), "call now \\(urgent\\)")


class BoldTests(unittest.TestCase):
    def test_double_star_to_v2_bold(self):
        self.assertEqual(to_markdown_v2("**bold** text"), "*bold* text")

    def test_double_underscore_to_v2_bold(self):
        self.assertEqual(to_markdown_v2("__bold__ text"), "*bold* text")

    def test_bold_with_period_after(self):
        self.assertEqual(to_markdown_v2("**bold**."), "*bold*\\.")

    def test_bold_inside_sentence(self):
        self.assertEqual(
            to_markdown_v2("the **important** thing"),
            "the *important* thing",
        )


class ItalicTests(unittest.TestCase):
    def test_single_star_to_underscore(self):
        self.assertEqual(to_markdown_v2("use *italic* now"), "use _italic_ now")

    def test_single_underscore_stays_underscore(self):
        self.assertEqual(to_markdown_v2("use _italic_ now"), "use _italic_ now")

    def test_arithmetic_star_not_italic(self):
        # `2 * 3` is multiplication, not italic.
        out = to_markdown_v2("compute 2 * 3 here")
        # Outer * are surrounded by space then digit — lookbehind/lookahead
        # block the italic match. The bare * gets escaped.
        self.assertNotIn("_3 here", out)
        self.assertIn("\\*", out)


class CodeTests(unittest.TestCase):
    def test_inline_code_preserved(self):
        self.assertEqual(to_markdown_v2("run `jc gateway` now"), "run `jc gateway` now")

    def test_inline_code_with_period_outside(self):
        self.assertEqual(
            to_markdown_v2("run `jc gateway`."),
            "run `jc gateway`\\.",
        )

    def test_inline_code_internal_backtick_escaped(self):
        self.assertEqual(to_markdown_v2("here `a\\b` end"), "here `a\\\\b` end")

    def test_code_fence_preserved(self):
        text = "```python\nx = 1\nprint(x)\n```"
        self.assertEqual(to_markdown_v2(text), text)

    def test_code_fence_no_lang(self):
        text = "```\nplain code\n```"
        self.assertEqual(to_markdown_v2(text), text)

    def test_code_fence_internal_backtick_escaped(self):
        text = "```\nx = `y`\n```"
        out = to_markdown_v2(text)
        self.assertIn("\\`y\\`", out)

    def test_code_fence_does_not_get_inside_processed(self):
        # `**bold**` inside fence must NOT become `*bold*` (that would
        # corrupt code semantics).
        text = "```\nuse **literal** stars\n```"
        out = to_markdown_v2(text)
        self.assertIn("**literal**", out)


class LinkTests(unittest.TestCase):
    def test_link_preserved(self):
        self.assertEqual(
            to_markdown_v2("see [docs](https://example.com)"),
            "see [docs](https://example.com)",
        )

    def test_link_with_underscore_in_url(self):
        # URL contains `_` which is V2-reserved in body but not in URL part.
        self.assertEqual(
            to_markdown_v2("[wiki](https://en.wikipedia.org/wiki/Foo_bar)"),
            "[wiki](https://en.wikipedia.org/wiki/Foo_bar)",
        )

    def test_link_text_period_escaped(self):
        self.assertEqual(
            to_markdown_v2("[docs.html](https://x.com/y)"),
            "[docs\\.html](https://x.com/y)",
        )

    def test_link_url_paren_escaped(self):
        out = to_markdown_v2("[w](https://example.com/foo(bar))")
        # Closing ) inside URL must be backslash-escaped per V2 rules.
        self.assertIn("\\)", out)


class HeadingAndListTests(unittest.TestCase):
    def test_heading_uppercased(self):
        out = to_markdown_v2("## Today's briefing\nbody")
        self.assertEqual(out.split("\n")[0], "TODAY'S BRIEFING")

    def test_h1_uppercased(self):
        out = to_markdown_v2("# Top\nsome body")
        self.assertTrue(out.startswith("TOP\n"))

    def test_bullet_dash_to_dot(self):
        out = to_markdown_v2("- one\n- two")
        # Each bullet line starts with `• `.
        self.assertIn("• one", out)
        self.assertIn("• two", out)

    def test_bullet_star_to_dot(self):
        out = to_markdown_v2("* one\n* two")
        self.assertIn("• one", out)
        self.assertIn("• two", out)


class StrikeTests(unittest.TestCase):
    def test_double_tilde_to_v2_strike(self):
        self.assertEqual(to_markdown_v2("~~struck~~"), "~struck~")


class MixedContentTests(unittest.TestCase):
    def test_mix_bold_italic_code_link_period(self):
        out = to_markdown_v2(
            "mix: **bold**, *italic*, `code`, and [link](https://x.com)."
        )
        self.assertIn("*bold*", out)
        self.assertIn("_italic_", out)
        self.assertIn("`code`", out)
        self.assertIn("[link](https://x.com)", out)
        self.assertTrue(out.endswith("\\."))

    def test_bare_url_chars_escaped(self):
        # Telegram still auto-detects URLs even when chars are escaped.
        out = to_markdown_v2("see https://en.wikipedia.org/wiki/Foo_(bar)")
        self.assertIn("\\.", out)
        self.assertIn("\\_", out)
        self.assertIn("\\(", out)
        self.assertIn("\\)", out)

    def test_telegram_reserved_chars_full_set(self):
        # Every V2-reserved char appears as literal at least once.
        text = "_*[]()~`>#+-=|{}.!"
        out = to_markdown_v2(text)
        # Each reserved char in the input should appear backslash-escaped
        # in the output. (Some chars open spans like `_x_` if matched, but
        # standalone they get escaped.)
        for ch in ".+-=|{}.!#>":
            self.assertIn(f"\\{ch}", out)


if __name__ == "__main__":
    unittest.main()
