"""Tests for email channel injection prevention."""

import unittest

from lib.channels.email.sanitize import sanitize_body, wrap_email_prompt, strip_html


class TestHTMLStripping(unittest.TestCase):
    def test_strip_simple_html(self):
        html = "<p>Hello <b>world</b></p>"
        result = strip_html(html)
        self.assertEqual(result, "Hello world")

    def test_strip_html_with_links(self):
        html = '<a href="http://evil.com">click here</a>'
        result = strip_html(html)
        self.assertEqual(result, "click here")

    def test_strip_html_preserves_text(self):
        html = "<div>Line 1</div><div>Line 2</div>"
        result = strip_html(html)
        self.assertIn("Line 1", result)
        self.assertIn("Line 2", result)


class TestBodySanitization(unittest.TestCase):
    def test_truncate_long_body(self):
        body = "x" * 10000
        result, was_truncated = sanitize_body(body, max_chars=8000)
        self.assertTrue(was_truncated)
        self.assertLessEqual(len(result), 8500)  # Allow some slack for truncation notice
        self.assertIn("truncated", result.lower())

    def test_preserve_short_body(self):
        body = "Short message"
        result, was_truncated = sanitize_body(body, max_chars=8000)
        self.assertFalse(was_truncated)
        self.assertEqual(result, body)

    def test_strip_html_during_sanitization(self):
        html = "<p>Hello <b>world</b></p>"
        result, _ = sanitize_body(html, is_html=True, max_chars=8000)
        self.assertEqual(result, "Hello world")

    def test_normalize_whitespace(self):
        body = "  Line 1  \n\n  Line 2  \n\n"
        result, _ = sanitize_body(body, max_chars=8000)
        self.assertEqual(result, "Line 1  \n\n  Line 2")


class TestEmailPromptWrapping(unittest.TestCase):
    def test_wrap_simple_email(self):
        prompt = wrap_email_prompt(
            sender="mario@scovai.com",
            subject="Hello",
            body="Test message",
        )
        self.assertIn("[EMAIL from mario@scovai.com", prompt)
        self.assertIn('subject: "Hello"', prompt)
        self.assertIn("Test message", prompt)

    def test_wrap_with_truncation_notice(self):
        body = "x" * 10000
        prompt = wrap_email_prompt(
            sender="mario@scovai.com",
            subject="Long email",
            body=body,
            max_chars=8000,
        )
        self.assertIn("truncated", prompt.lower())

    def test_wrap_html_email(self):
        html_body = "<p>Hello <b>world</b></p>"
        prompt = wrap_email_prompt(
            sender="mario@scovai.com",
            subject="HTML Email",
            body=html_body,
            is_html=True,
        )
        # HTML should be stripped
        self.assertNotIn("<p>", prompt)
        self.assertNotIn("<b>", prompt)
        self.assertIn("Hello world", prompt)


if __name__ == "__main__":
    unittest.main()
