"""Regression tests for non-destructive `jc upgrade`.

Covers docs/specs/jc-upgrade-preserve-operator-blocks.md:

- operator-owned top-level config survives upgrade
- channel child fields not owned by prompts survive upgrade
- validation failure aborts before replacing gateway.yaml
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
UPGRADE_BIN = REPO_ROOT / "bin" / "jc-upgrade"


def _run_upgrade(instance: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin"
    env["PYTHON"] = str(REPO_ROOT / ".venv" / "bin" / "python")
    return subprocess.run(
        [str(UPGRADE_BIN), "--instance-dir", str(instance), "--defaults"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _write_instance(tmp_path: Path, gateway_yaml: str) -> Path:
    instance = tmp_path / "instance"
    (instance / "ops").mkdir(parents=True)
    (instance / "ops" / "gateway.yaml").write_text(gateway_yaml, encoding="utf-8")
    (instance / ".env").write_text(
        "TELEGRAM_BOT_TOKEN='token'\n"
        "TELEGRAM_CHAT_ID='111'\n"
        "OPENROUTER_API_KEY='or-key'\n",
        encoding="utf-8",
    )
    return instance


def test_upgrade_preserves_operator_owned_gateway_blocks(tmp_path: Path) -> None:
    instance = _write_instance(
        tmp_path,
        """
default_brain: claude
timezone: Asia/Dubai
gateway:
  poll_interval_seconds: 7
  lease_seconds: 999
triage: openrouter
triage_confidence_threshold: 0.6
default_fallback_brain: claude:sonnet
sticky_brain_idle_timeout_seconds: 12
triage_routing:
  smalltalk: claude:haiku
  quick: claude:sonnet
  analysis: claude:opus
  code: claude:sonnet
  image: claude:sonnet
  voice: claude:sonnet
  system: claude:haiku
openrouter_model: x-ai/grok-4.1-fast
openrouter_api_key_env: OPENROUTER_API_KEY
openrouter_timeout_seconds: 9
reply_footer:
  enabled: true
  show_session: false
reliability:
  max_queue_depth: 123
  log_backups: 8
brains:
  opencode:
    timeout_seconds: 90
channels:
  telegram:
    enabled: true
    token_env: TELEGRAM_BOT_TOKEN
    chat_ids: ["111", "222"]
    blocked_chat_ids: ["999"]
    brain: claude:sonnet
  email:
    enabled: true
    senders:
      trusted: ["operator@example.com"]
      external: []
      blocklist: ["spam@example.com"]
    approvals:
      notify_on_external: false
      notify_on_draft: true
  voice:
    enabled: false
    paired_with: telegram
    asr_provider: dashscope
    tts_provider: dashscope
""".lstrip(),
    )

    result = _run_upgrade(instance)
    assert result.returncode == 0, result.stderr

    data = yaml.safe_load((instance / "ops" / "gateway.yaml").read_text(encoding="utf-8"))

    assert data["reply_footer"]["enabled"] is True
    assert data["reply_footer"]["show_session"] is False
    assert data["reliability"]["max_queue_depth"] == 123
    assert data["reliability"]["log_backups"] == 8
    assert data["brains"]["opencode"]["timeout_seconds"] == 90
    assert data["gateway"]["poll_interval_seconds"] == 7
    assert data["gateway"]["lease_seconds"] == 999

    telegram = data["channels"]["telegram"]
    assert telegram["chat_ids"] == ["111", "222"]
    assert telegram["blocked_chat_ids"] == ["999"]
    assert telegram["brain"] == "claude:sonnet"

    email = data["channels"]["email"]
    assert email["enabled"] is True
    assert email["senders"]["trusted"] == ["operator@example.com"]
    assert email["senders"]["blocklist"] == ["spam@example.com"]
    assert email["approvals"]["notify_on_external"] is False

    assert data["triage_routing"]["analysis"] == "claude:opus"
    assert data["openrouter_model"] == "x-ai/grok-4.1-fast"
    backups = list((instance / "ops").glob("gateway.yaml.bak.*"))
    assert backups, "upgrade should leave a rollback backup"


def test_upgrade_defaults_preserve_default_model_and_disabled_telegram(tmp_path: Path) -> None:
    instance = _write_instance(
        tmp_path,
        """
default_brain: claude
default_model: sonnet-4-6
timezone: Asia/Dubai
triage: none
channels:
  telegram:
    enabled: false
    token_env: TELEGRAM_BOT_TOKEN
    chat_ids: ["111"]
""".lstrip(),
    )

    result = _run_upgrade(instance)
    assert result.returncode == 0, result.stderr

    data = yaml.safe_load((instance / "ops" / "gateway.yaml").read_text(encoding="utf-8"))

    assert data["default_brain"] == "claude"
    assert data["default_model"] == "sonnet-4-6"
    assert data["channels"]["telegram"]["enabled"] is False


def test_upgrade_clears_default_model_when_default_brain_embeds_model(tmp_path: Path) -> None:
    instance = _write_instance(
        tmp_path,
        """
default_brain: claude:sonnet-4-6
default_model: haiku-4-5
timezone: Asia/Dubai
triage: none
channels:
  telegram:
    enabled: false
""".lstrip(),
    )

    result = _run_upgrade(instance)
    assert result.returncode == 0, result.stderr

    data = yaml.safe_load((instance / "ops" / "gateway.yaml").read_text(encoding="utf-8"))

    assert data["default_brain"] == "claude:sonnet-4-6"
    assert data["default_model"] is None


def test_upgrade_defaults_preserve_nested_triage_config(tmp_path: Path) -> None:
    instance = _write_instance(
        tmp_path,
        """
default_brain: claude
timezone: Asia/Dubai
triage:
  backend: api_classifier
  protocol: openai_compat
  base_url: https://router.example.test/v1
  api_key_env: ROUTER_API_KEY
  model: x-ai/grok-4.1-fast
  timeout_seconds: 11
  routing:
    smalltalk: opencode:deepseek/deepseek-v4-flash
    quick: opencode:deepseek/deepseek-v4-flash
    analysis: opencode:deepseek/deepseek-v4-pro
channels:
  telegram:
    enabled: false
""".lstrip(),
    )

    result = _run_upgrade(instance)
    assert result.returncode == 0, result.stderr

    data = yaml.safe_load((instance / "ops" / "gateway.yaml").read_text(encoding="utf-8"))

    assert data["triage"]["backend"] == "api_classifier"
    assert data["triage"]["base_url"] == "https://router.example.test/v1"
    assert data["triage"]["model"] == "x-ai/grok-4.1-fast"
    assert data["triage"]["routing"]["quick"] == "opencode:deepseek/deepseek-v4-flash"
    assert "triage_routing" not in data
    assert "openrouter_model" not in data


def test_upgrade_validation_failure_leaves_gateway_yaml_unchanged(tmp_path: Path) -> None:
    original = """
default_brain: definitely-not-a-brain
timezone: UTC
triage: none
channels:
  telegram:
    enabled: false
""".lstrip()
    instance = _write_instance(tmp_path, original)

    result = _run_upgrade(instance)

    assert result.returncode == 1
    assert "failed validation" in result.stderr
    assert (instance / "ops" / "gateway.yaml").read_text(encoding="utf-8") == original
    assert list((instance / "ops").glob("gateway.yaml.bak.*"))
