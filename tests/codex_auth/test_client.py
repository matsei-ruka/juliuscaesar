"""Unit tests for `lib/codex_auth/client.py`.

Mocks the HTTP refresh endpoint so the suite never touches the network.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from codex_auth.client import (
    CodexAuthClient,
    DEFAULT_CLIENT_ID,
    _file_lock,
    _state_with_new_tokens,
    jwt_unverified_exp,
    jwt_unverified_payload,
)
from codex_auth.errors import (
    AuthFileCorrupt,
    AuthFileMissing,
    AuthModeUnsupported,
    RefreshExpired,
    RefreshFailed,
)


def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def make_jwt(*, exp: float, client_id: str = DEFAULT_CLIENT_ID, plan: str = "plus") -> str:
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "exp": int(exp),
        "iat": int(exp - 600),
        "aud": ["https://api.openai.com/v1"],
        "client_id": client_id,
        "scp": ["openid", "profile", "email", "offline_access"],
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": plan,
            "chatgpt_account_id": "acc-uuid-1",
        },
    }
    h = _b64url(json.dumps(header).encode()).decode()
    p = _b64url(json.dumps(payload).encode()).decode()
    return f"{h}.{p}.signature"


def write_auth_file(
    path: Path,
    *,
    access_exp: float | None = None,
    refresh_token: str = "rt_abc",
    auth_mode: str = "chatgpt",
    extra: dict | None = None,
) -> None:
    if access_exp is None:
        access_exp = time.time() + 9 * 86400
    payload = {
        "auth_mode": auth_mode,
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "id." + _b64url(b'{"sub":"u"}').decode() + ".sig",
            "access_token": make_jwt(exp=access_exp),
            "refresh_token": refresh_token,
            "account_id": "acc-uuid-1",
        },
        "last_refresh": "2026-04-24T12:15:25Z",
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    path.chmod(0o600)


class FakeOpener:
    """Mock URL opener — records calls, returns scripted responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post_form(self, url, body, timeout):
        self.calls.append({"url": url, "body": dict(body), "timeout": timeout})
        if not self._responses:
            return 500, b'{"error":"no more responses"}'
        return self._responses.pop(0)


class JwtDecodeTests(unittest.TestCase):
    def test_decode_payload(self):
        token = make_jwt(exp=1_700_000_000)
        payload = jwt_unverified_payload(token)
        self.assertEqual(payload["client_id"], DEFAULT_CLIENT_ID)
        self.assertEqual(payload["exp"], 1_700_000_000)

    def test_exp_extraction(self):
        self.assertEqual(jwt_unverified_exp(make_jwt(exp=42)), 42.0)

    def test_garbage_returns_none(self):
        self.assertIsNone(jwt_unverified_payload("not.a.jwt"))
        self.assertIsNone(jwt_unverified_payload("abc"))
        self.assertIsNone(jwt_unverified_payload(""))
        self.assertIsNone(jwt_unverified_exp("not.a.jwt"))


class StatusAndReadTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.auth = Path(self.tmpdir.name) / "auth.json"

    def test_missing_file_raises(self):
        client = CodexAuthClient(auth_file=self.auth)
        with self.assertRaises(AuthFileMissing):
            client.read_state()

    def test_status_snapshot_no_token_leak(self):
        write_auth_file(self.auth)
        client = CodexAuthClient(auth_file=self.auth)
        snap = client.status()
        # Spec: status command MUST NOT include the bearer token itself.
        flat = json.dumps(snap)
        self.assertNotIn("access_token", flat)
        self.assertNotIn(snap["client_id"], "<no client_id>")
        self.assertEqual(snap["plan"], "plus")
        self.assertEqual(snap["auth_mode"], "chatgpt")
        self.assertGreater(snap["expires_in_seconds"], 0)

    def test_corrupt_file_raises(self):
        self.auth.write_text("{not json", encoding="utf-8")
        client = CodexAuthClient(auth_file=self.auth)
        with self.assertRaises(AuthFileCorrupt):
            client.read_state()

    def test_api_key_mode_rejected(self):
        write_auth_file(self.auth, auth_mode="apikey")
        client = CodexAuthClient(auth_file=self.auth)
        with self.assertRaises(AuthModeUnsupported):
            client.read_state()

    def test_get_bearer_returns_unmodified_when_far_from_expiry(self):
        write_auth_file(self.auth, access_exp=time.time() + 9 * 86400)
        opener = FakeOpener([])
        client = CodexAuthClient(auth_file=self.auth, opener=opener)
        token = client.get_bearer()
        self.assertTrue(token)
        self.assertEqual(opener.calls, [])  # no refresh fired


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.auth = Path(self.tmpdir.name) / "auth.json"

    def _success_response(self, *, access_exp_offset: float = 9 * 86400):
        new_token = make_jwt(exp=time.time() + access_exp_offset)
        body = json.dumps(
            {
                "access_token": new_token,
                "id_token": "newid.body.sig",
                "refresh_token": "rt_new",
                "expires_in": int(access_exp_offset),
                "token_type": "Bearer",
            }
        ).encode()
        return 200, body, new_token

    def test_refresh_skew_triggers_refresh(self):
        # Token expires in 60s; default skew is 300s -> refresh required.
        write_auth_file(self.auth, access_exp=time.time() + 60)
        status, body, new_token = self._success_response()
        opener = FakeOpener([(status, body)])
        client = CodexAuthClient(auth_file=self.auth, opener=opener)
        token = client.get_bearer()
        self.assertEqual(token, new_token)
        self.assertEqual(len(opener.calls), 1)
        sent = opener.calls[0]["body"]
        self.assertEqual(sent["grant_type"], "refresh_token")
        self.assertEqual(sent["refresh_token"], "rt_abc")
        self.assertEqual(sent["client_id"], DEFAULT_CLIENT_ID)

    def test_refresh_writes_atomically_and_preserves_mode(self):
        write_auth_file(self.auth, access_exp=time.time() + 60)
        status, body, new_token = self._success_response()
        opener = FakeOpener([(status, body)])
        client = CodexAuthClient(auth_file=self.auth, opener=opener)
        client.get_bearer()
        # Original mode 0600 must survive the rewrite.
        self.assertEqual(stat.S_IMODE(self.auth.stat().st_mode), 0o600)
        # New tokens must have landed in the file.
        on_disk = json.loads(self.auth.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["tokens"]["access_token"], new_token)
        self.assertEqual(on_disk["tokens"]["refresh_token"], "rt_new")
        self.assertNotEqual(on_disk["last_refresh"], "2026-04-24T12:15:25Z")

    def test_refresh_keeps_old_refresh_token_when_response_omits_it(self):
        write_auth_file(self.auth, access_exp=time.time() + 60)
        new_token = make_jwt(exp=time.time() + 9 * 86400)
        body = json.dumps(
            {"access_token": new_token, "id_token": "id.body.sig", "expires_in": 600000}
        ).encode()
        opener = FakeOpener([(200, body)])
        client = CodexAuthClient(auth_file=self.auth, opener=opener)
        client.get_bearer()
        on_disk = json.loads(self.auth.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["tokens"]["refresh_token"], "rt_abc")  # rotated only when present

    def test_invalid_grant_translates_to_relogin_required(self):
        write_auth_file(self.auth, access_exp=time.time() + 60)
        body = json.dumps({"error": "invalid_grant", "error_description": "expired"}).encode()
        opener = FakeOpener([(400, body)])
        client = CodexAuthClient(auth_file=self.auth, opener=opener)
        with self.assertRaises(RefreshExpired) as cm:
            client.get_bearer()
        self.assertEqual(cm.exception.code, "invalid_grant")

    def test_5xx_retries_then_surfaces(self):
        write_auth_file(self.auth, access_exp=time.time() + 60)
        opener = FakeOpener([(500, b"")] * 8)  # exhaust retries
        client = CodexAuthClient(auth_file=self.auth, opener=opener)
        # We cap retries at 4 attempts (3 backoffs + final). The last attempt raises.
        with self.assertRaises(RefreshFailed):
            with _NoSleep():
                client.get_bearer()
        # 4 attempts total per RETRY_BACKOFF_SECONDS.
        self.assertEqual(len(opener.calls), 4)

    def test_force_refresh_skips_skew_check(self):
        # Token still has plenty of lifetime; force_refresh should still hit the wire.
        write_auth_file(self.auth, access_exp=time.time() + 9 * 86400)
        status, body, new_token = self._success_response()
        opener = FakeOpener([(status, body)])
        client = CodexAuthClient(auth_file=self.auth, opener=opener)
        state = client.force_refresh()
        self.assertEqual(state.access_token, new_token)
        self.assertEqual(len(opener.calls), 1)

    def test_double_checked_refresh_skips_when_other_writer_already_did(self):
        # Simulate: thread A calls get_bearer at the moment B finishes refresh.
        # We approximate by writing a fresh token to the file *before* the
        # refresh path acquires its exclusive lock. Inside the locked section
        # the client should re-read, see the token is fine, and return early.
        write_auth_file(self.auth, access_exp=time.time() + 60)
        client = CodexAuthClient(auth_file=self.auth, opener=FakeOpener([]))
        # Pre-empt the refresh by writing a fresh token now.
        future = time.time() + 9 * 86400
        write_auth_file(self.auth, access_exp=future)
        token = client.get_bearer()
        self.assertTrue(token)


class FileLockContentionTests(unittest.TestCase):
    """Two threads racing into _file_lock — second blocks until first releases."""

    def test_exclusive_lock_blocks_concurrent_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "auth.json.lock"
            order: list[str] = []
            ready = threading.Event()
            release_first = threading.Event()

            def first():
                with _file_lock(lock_path, exclusive=True, timeout=5):
                    order.append("first-acquired")
                    ready.set()
                    release_first.wait(timeout=5)
                    order.append("first-releasing")

            def second():
                ready.wait(timeout=5)
                with _file_lock(lock_path, exclusive=True, timeout=5):
                    order.append("second-acquired")

            t1 = threading.Thread(target=first)
            t2 = threading.Thread(target=second)
            t1.start()
            t2.start()
            time.sleep(0.05)
            self.assertEqual(order, ["first-acquired"])
            release_first.set()
            t1.join(timeout=5)
            t2.join(timeout=5)
            self.assertEqual(order, ["first-acquired", "first-releasing", "second-acquired"])


class StateRoundTripTests(unittest.TestCase):
    def test_unknown_keys_preserved_through_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            write_auth_file(auth, extra={"unknown_codex_field": {"keep": True}})
            client = CodexAuthClient(auth_file=auth)
            state = client.read_state()
            updated = _state_with_new_tokens(
                state,
                access_token=make_jwt(exp=time.time() + 9 * 86400),
                id_token=None,
                refresh_token=None,
                last_refresh_iso="2099-01-01T00:00:00Z",
            )
            self.assertEqual(updated.raw["unknown_codex_field"], {"keep": True})


class _NoSleep:
    """Patch time.sleep within client.py to skip retry backoff in tests."""

    def __enter__(self):
        import codex_auth.client as mod

        self._orig = mod.time.sleep
        mod.time.sleep = lambda *_a, **_kw: None  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        import codex_auth.client as mod

        mod.time.sleep = self._orig
        return False


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
