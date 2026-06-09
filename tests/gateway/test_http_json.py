"""Tests for the shared http_json helper.

Focus: 4xx responses from JSON APIs (Telegram/Discord/Slack) carry a
structured error body that callers branch on. urllib raises HTTPError before
the caller can read it, so http_json must surface the JSON body instead of
propagating the exception — otherwise no-op edit detection and parse_mode
fallback in supervisor.delivery never run and every 400 spawns an orphan card.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.channels._http import http_json  # noqa: E402


def _http_error(url: str, code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=url, code=code, msg="Bad Request", hdrs=None, fp=io.BytesIO(body)
    )


class HttpJsonErrorBodyTests(unittest.TestCase):
    def test_400_with_json_body_is_returned_not_raised(self) -> None:
        body = json.dumps(
            {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: message is not modified",
            }
        ).encode("utf-8")
        with patch(
            "gateway.channels._http.urllib.request.urlopen",
            side_effect=_http_error("https://api.telegram.org/x", 400, body),
        ):
            data = http_json("https://api.telegram.org/x", data={"a": 1})
        self.assertFalse(data["ok"])
        self.assertIn("not modified", data["description"])

    def test_400_with_parse_error_body_is_returned(self) -> None:
        body = json.dumps(
            {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}
        ).encode("utf-8")
        with patch(
            "gateway.channels._http.urllib.request.urlopen",
            side_effect=_http_error("https://api.telegram.org/x", 400, body),
        ):
            data = http_json("https://api.telegram.org/x", data={"a": 1})
        self.assertIn("entit", data["description"].lower())

    def test_4xx_with_non_json_body_reraises(self) -> None:
        with patch(
            "gateway.channels._http.urllib.request.urlopen",
            side_effect=_http_error("https://x/y", 404, b"<html>not found</html>"),
        ):
            with self.assertRaises(urllib.error.HTTPError):
                http_json("https://x/y")

    def test_4xx_with_empty_body_reraises(self) -> None:
        with patch(
            "gateway.channels._http.urllib.request.urlopen",
            side_effect=_http_error("https://x/y", 500, b""),
        ):
            with self.assertRaises(urllib.error.HTTPError):
                http_json("https://x/y")


if __name__ == "__main__":
    unittest.main()
