"""Tests for the registration round-trip flow.

Mocks ``requests.Session.request`` (or ``post``) so we can verify the
client sends the right body and writes the returned API key to .env.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from company import conf as company_conf  # noqa: E402
from company.client import CompanyClient, CompanyError  # noqa: E402
from company.reporter import Reporter  # noqa: E402
from gateway import config as gw_config  # noqa: E402


def _make_instance(tmp: str, env_lines: list[str] | None = None) -> Path:
    instance = Path(tmp)
    (instance / "ops").mkdir()
    (instance / "memory" / "L1").mkdir(parents=True)
    (instance / "memory" / "L1" / "IDENTITY.md").write_text(
        "# Rachel Zane\n", encoding="utf-8"
    )
    (instance / "ops" / "gateway.yaml").write_text("default_brain: claude\n", encoding="utf-8")
    if env_lines:
        (instance / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    return instance


def _ok_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = json.dumps(payload).encode()
    resp.text = json.dumps(payload)
    resp.json.return_value = payload
    resp.request.method = "POST"
    resp.url = "http://x"
    return resp


def _err_response(status: int, body: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = body.encode()
    resp.text = body
    resp.json.side_effect = ValueError
    resp.request.method = "POST"
    resp.url = "http://x"
    return resp


class WriteEnvPermsTests(unittest.TestCase):
    @unittest.skipIf(__import__("os").name == "nt", "POSIX perms only")
    def test_env_file_perms_are_0600(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            company_conf.write_env_keys(
                instance, set_keys={"COMPANY_API_KEY": "secret"}
            )
            mode = (instance / ".env").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

            # Updating an existing file must also land at 0o600 even if the
            # caller previously made it world-readable.
            os.chmod(instance / ".env", 0o644)
            company_conf.write_env_keys(
                instance, set_keys={"COMPANY_API_KEY": "secret2"}
            )
            mode = (instance / ".env").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)


class CompanyClientRegisterTests(unittest.TestCase):
    def setUp(self) -> None:
        gw_config.clear_env_cache()

    def test_register_sends_expected_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp,
                env_lines=[
                    "COMPANY_ENDPOINT=http://192.168.3.246:8080",
                    "COMPANY_ENROLLMENT_TOKEN=tok-1",
                ],
            )
            cfg = company_conf.load(instance)
            session = MagicMock()
            session.post.return_value = _ok_response(
                {"agent_id": "agent-uuid", "api_key": "shiny-key"}
            )
            client = CompanyClient(cfg, session=session)

            result = client.register(
                instance_id="iid",
                name="Rachel Zane",
                framework="juliuscaesar",
                framework_version="2026.04.28",
                enrollment_token="tok-1",
            )

            self.assertEqual(result["api_key"], "shiny-key")
            session.post.assert_called_once()
            kwargs = session.post.call_args.kwargs
            self.assertEqual(
                kwargs["json"],
                {
                    "instance_id": "iid",
                    "name": "Rachel Zane",
                    "framework": "juliuscaesar",
                    "framework_version": "2026.04.28",
                    "enrollment_token": "tok-1",
                },
            )
            # Register must NOT carry the (still-empty) Authorization header.
            self.assertNotIn("Authorization", kwargs["headers"])

    def test_non_2xx_raises_company_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp,
                env_lines=["COMPANY_ENDPOINT=http://x", "COMPANY_API_KEY=k"],
            )
            cfg = company_conf.load(instance)
            session = MagicMock()
            session.post.return_value = _err_response(401, "unauthorized")
            client = CompanyClient(cfg, session=session)

            with self.assertRaises(CompanyError) as ctx:
                client.post_alert({"severity": "info", "title": "x"})
            self.assertEqual(ctx.exception.status, 401)


class ReporterRegistrationTests(unittest.TestCase):
    """End-to-end: token-only .env -> Reporter._register -> key persisted."""

    def setUp(self) -> None:
        gw_config.clear_env_cache()

    def test_register_persists_key_and_strips_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp,
                env_lines=[
                    "COMPANY_ENDPOINT=http://192.168.3.246:8080",
                    "COMPANY_ENROLLMENT_TOKEN=tok-1",
                ],
            )
            with patch("company.reporter.CompanyClient") as MockClient:
                fake = MagicMock()
                fake.register.return_value = {
                    "agent_id": "agent-uuid",
                    "api_key": "fresh-key",
                }
                MockClient.return_value = fake

                reporter = Reporter(instance)
                reporter._register()

            text = (instance / ".env").read_text(encoding="utf-8")
            self.assertIn("COMPANY_API_KEY=fresh-key", text)
            self.assertNotIn("COMPANY_ENROLLMENT_TOKEN", text)
            # Reporter cfg refreshed in-place.
            self.assertEqual(reporter.cfg.api_key, "fresh-key")
            self.assertEqual(reporter.cfg.enrollment_token, "")

    def test_register_no_api_key_in_response_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp,
                env_lines=[
                    "COMPANY_ENDPOINT=http://x",
                    "COMPANY_ENROLLMENT_TOKEN=tok",
                ],
            )
            with patch("company.reporter.CompanyClient") as MockClient:
                fake = MagicMock()
                fake.register.return_value = {"agent_id": "x"}  # missing api_key
                MockClient.return_value = fake

                reporter = Reporter(instance)
                with self.assertRaises(CompanyError):
                    reporter._register()


if __name__ == "__main__":
    unittest.main()
