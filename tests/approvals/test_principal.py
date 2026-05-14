"""Principal-resolution fallback chain."""

from __future__ import annotations

from pathlib import Path

import pytest

from approvals.principal import load_principal


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("TELEGRAM_CHAT_ID", "TELEGRAM_USER_ID", "PRINCIPAL_EMAIL"):
        monkeypatch.delenv(key, raising=False)


def test_principal_from_yaml(instance_dir: Path) -> None:
    (instance_dir / "ops" / "gateway.yaml").write_text(
        """\
principal:
  telegram_chat_id: 28547271
  telegram_user_id: 28547271
  email: luca@example.com
  email_domain: example.com
""",
        encoding="utf-8",
    )
    p = load_principal(instance_dir)
    assert p.telegram_chat_id == "28547271"
    assert p.email == "luca@example.com"
    assert p.email_domain == "example.com"


def test_principal_from_env_only(instance_dir: Path) -> None:
    (instance_dir / ".env").write_text(
        "TELEGRAM_CHAT_ID=42\nPRINCIPAL_EMAIL=op@example.com\n",
        encoding="utf-8",
    )
    p = load_principal(instance_dir)
    assert p.telegram_chat_id == "42"
    assert p.email == "op@example.com"
    assert p.email_domain == "example.com"


def test_principal_yaml_overrides_env(instance_dir: Path) -> None:
    (instance_dir / ".env").write_text("TELEGRAM_CHAT_ID=999\n", encoding="utf-8")
    (instance_dir / "ops" / "gateway.yaml").write_text(
        "principal:\n  telegram_chat_id: 1\n",
        encoding="utf-8",
    )
    p = load_principal(instance_dir)
    assert p.telegram_chat_id == "1"


def test_principal_missing(instance_dir: Path) -> None:
    p = load_principal(instance_dir)
    assert p.telegram_chat_id is None
    assert p.email is None
