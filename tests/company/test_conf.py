"""Tests for ``company.conf`` config + .env handling."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from company import conf as company_conf  # noqa: E402
from gateway import config as gw_config  # noqa: E402


def _make_instance(tmp: str, *, env_lines: list[str] | None = None, gateway_yaml: str = "") -> Path:
    instance = Path(tmp)
    (instance / "ops").mkdir()
    (instance / "memory" / "L1").mkdir(parents=True)
    if gateway_yaml:
        (instance / "ops" / "gateway.yaml").write_text(gateway_yaml, encoding="utf-8")
    if env_lines:
        (instance / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    return instance


class LoadTests(unittest.TestCase):
    def setUp(self) -> None:
        gw_config.clear_env_cache()

    def test_load_with_no_env_returns_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            cfg = company_conf.load(instance)
            self.assertEqual(cfg.endpoint, "")
            self.assertEqual(cfg.api_key, "")
            self.assertFalse(company_conf.is_enabled(instance))

    def test_load_picks_up_endpoint_and_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp,
                env_lines=[
                    "COMPANY_ENDPOINT=http://192.168.3.246:8080",
                    "COMPANY_API_KEY=abc123",
                ],
            )
            cfg = company_conf.load(instance)
            self.assertEqual(cfg.endpoint, "http://192.168.3.246:8080")
            self.assertEqual(cfg.api_key, "abc123")
            self.assertTrue(company_conf.is_enabled(instance))

    def test_enrollment_token_alone_enables(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp,
                env_lines=[
                    "COMPANY_ENDPOINT=http://x",
                    "COMPANY_ENROLLMENT_TOKEN=tok-xyz",
                ],
            )
            self.assertTrue(company_conf.is_enabled(instance))

    def test_yaml_block_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml = (
                "default_brain: claude\n"
                "company:\n"
                "  enabled: true\n"
                "  redact_conversations: false\n"
                "  exclude_channels: [voice]\n"
                "  conversation_max_chars: 1000\n"
                "  outbox_max_mb: 50\n"
            )
            instance = _make_instance(
                tmp,
                env_lines=["COMPANY_ENDPOINT=http://x", "COMPANY_API_KEY=k"],
                gateway_yaml=yaml,
            )
            cfg = company_conf.load(instance)
            self.assertFalse(cfg.redact_conversations)
            self.assertEqual(cfg.exclude_channels, ("voice",))
            self.assertEqual(cfg.conversation_max_chars, 1000)
            self.assertEqual(cfg.outbox_max_mb, 50)

    def test_disabled_block_means_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml = "default_brain: claude\ncompany:\n  enabled: false\n"
            instance = _make_instance(
                tmp,
                env_lines=["COMPANY_ENDPOINT=http://x", "COMPANY_API_KEY=k"],
                gateway_yaml=yaml,
            )
            self.assertFalse(company_conf.is_enabled(instance))


class IdentityTests(unittest.TestCase):
    def test_instance_id_is_stable_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            a = company_conf.instance_id(instance)
            b = company_conf.instance_id(instance)
            self.assertEqual(a, b)
            self.assertEqual(len(a), 64)

    def test_instance_name_reads_first_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            (instance / "memory" / "L1" / "IDENTITY.md").write_text(
                "# Rachel Zane\n\nbody\n", encoding="utf-8"
            )
            self.assertEqual(company_conf.instance_name(instance), "Rachel Zane")

    def test_instance_name_falls_back_to_dirname(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create predictable basename via nested dir.
            nested = Path(tmp) / "myagent"
            nested.mkdir()
            (nested / "memory" / "L1").mkdir(parents=True)
            self.assertEqual(company_conf.instance_name(nested), "myagent")


class FrameworkVersionTests(unittest.TestCase):
    """`framework_version()` composes pyproject + git SHA + dirty flag."""

    def test_returns_pyproject_version_in_real_repo(self):
        # In CI / local dev we're inside a git checkout. The string
        # should at minimum carry the pyproject version, and very
        # likely a `+sha` suffix too.
        out = company_conf.framework_version()
        # base version is dotted "YYYY.MM.DD.N" or similar; the
        # invariant we can assert: not empty, not the literal fallback.
        self.assertTrue(out, "framework_version() returned empty string")
        self.assertNotEqual(out, "0.0.0")

    def test_read_pyproject_version_parses_quoted_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "pyproject.toml"
            p.write_text('[project]\nname = "x"\nversion = "1.2.3"\n', encoding="utf-8")
            self.assertEqual(
                company_conf._read_pyproject_version(p),
                "1.2.3",
            )

    def test_read_pyproject_version_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "pyproject.toml"  # not created
            self.assertIsNone(company_conf._read_pyproject_version(p))

    def test_git_helpers_return_safely_outside_git(self):
        # tempdir is not a git repo → both helpers must not raise.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(company_conf._git_short_sha(Path(tmp)), "")
            self.assertFalse(company_conf._git_is_dirty(Path(tmp)))


class WriteEnvTests(unittest.TestCase):
    def test_write_env_keys_creates_file_with_safe_perms(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            company_conf.write_env_keys(instance, set_keys={"COMPANY_API_KEY": "abc"})
            text = (instance / ".env").read_text(encoding="utf-8")
            self.assertIn("COMPANY_API_KEY=abc", text)

    def test_write_env_keys_replaces_and_unsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(
                tmp,
                env_lines=[
                    "OTHER=keep",
                    "COMPANY_ENROLLMENT_TOKEN=old",
                    "COMPANY_API_KEY=stale",
                ],
            )
            company_conf.write_env_keys(
                instance,
                set_keys={"COMPANY_API_KEY": "new"},
                unset_keys=("COMPANY_ENROLLMENT_TOKEN",),
            )
            text = (instance / ".env").read_text(encoding="utf-8")
            self.assertIn("OTHER=keep", text)
            self.assertIn("COMPANY_API_KEY=new", text)
            self.assertNotIn("stale", text)
            self.assertNotIn("COMPANY_ENROLLMENT_TOKEN", text)


class GatewayYamlValidatorTests(unittest.TestCase):
    """Make sure the validator accepts (and rejects) company: blocks correctly."""

    def test_unknown_field_in_company_block_fails(self):
        from gateway.config import _validate_raw_config, ConfigError

        data = {"company": {"enabled": True, "bogus": 1}}
        with self.assertRaises(ConfigError):
            _validate_raw_config(data)

    def test_known_fields_pass(self):
        from gateway.config import _validate_raw_config

        data = {
            "company": {
                "enabled": True,
                "redact_conversations": False,
                "exclude_channels": ["voice"],
                "exclude_users": ["123"],
                "conversation_max_chars": 100,
                "outbox_max_mb": 10,
                "outbox_max_age_hours": 12,
            }
        }
        _validate_raw_config(data)

    def test_email_approval_fields_pass(self):
        from gateway.config import _validate_raw_config

        data = {
            "channels": {
                "email": {
                    "enabled": True,
                    "senders": {"trusted": [], "external": [], "blocklist": []},
                    "approvals": {
                        "notify_on_external": True,
                        "notify_on_draft": True,
                        "telegram_chat_id": None,
                    },
                }
            }
        }
        _validate_raw_config(data)


if __name__ == "__main__":
    unittest.main()
