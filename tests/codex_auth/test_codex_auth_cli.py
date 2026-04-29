"""Smoke tests for `bin/jc-codex-auth`'s CLI implementation.

Drives :func:`codex_auth.cli.main` directly so we never touch the network.
"""

from __future__ import annotations

import io
import json
import time
import unittest
import unittest.mock as mock
from pathlib import Path

from codex_auth.cli import main

from tests.codex_auth.test_client import write_auth_file


class CliSmokeTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth = Path(self.tmp.name) / "auth.json"
        write_auth_file(self.auth, access_exp=time.time() + 9 * 86400)

    def _run(self, *argv) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = main(["--auth-file", str(self.auth), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_status_text(self):
        rc, out, err = self._run("status")
        self.assertEqual(rc, 0)
        self.assertIn("chatgpt", out)
        self.assertIn("expires in", out)
        self.assertIn("mode 600", out)
        self.assertNotIn("eyJ", out)  # no JWT leakage
        self.assertNotIn("rt_", out)

    def test_status_json(self):
        rc, out, _ = self._run("status", "--json")
        self.assertEqual(rc, 0)
        snapshot = json.loads(out)
        self.assertEqual(snapshot["plan"], "plus")
        self.assertEqual(snapshot["auth_mode"], "chatgpt")

    def test_token_prints_jwt(self):
        rc, out, _ = self._run("token")
        self.assertEqual(rc, 0)
        token = out.strip()
        self.assertTrue(token)
        self.assertEqual(token.count("."), 2)  # JWT shape

    def test_status_missing_file_exits_2(self):
        missing = Path(self.tmp.name) / "absent.json"
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = main(["--auth-file", str(missing), "status"])
        self.assertEqual(rc, 2)
        self.assertIn("not found", err.getvalue())

    def test_apikey_mode_rejected(self):
        write_auth_file(self.auth, auth_mode="apikey", access_exp=time.time() + 9 * 86400)
        rc, _, err = self._run("status")
        self.assertEqual(rc, 2)
        self.assertIn("auth_mode", err)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
