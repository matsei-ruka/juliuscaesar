"""Tests for supervisor config loader."""

from pathlib import Path

from supervisor.config import load_config


def _write_gateway_yaml(instance_dir: Path, content: str) -> None:
    ops = instance_dir / "ops"
    ops.mkdir(parents=True, exist_ok=True)
    (ops / "gateway.yaml").write_text(content)


def test_defaults_when_no_config(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.enabled is True
    assert cfg.notice_threshold("claude") == 30
    assert cfg.notice_threshold("codex") == 90
    assert cfg.notice_threshold("pi") == 45
    assert cfg.notice_threshold("unknown_brain") == 60
    assert cfg.channel_enabled("telegram") is True
    assert cfg.channel_enabled("voice") is False


def test_enabled_false(tmp_path):
    _write_gateway_yaml(tmp_path, "supervisor:\n  enabled: false\n")
    assert load_config(tmp_path).enabled is False


def test_enabled_true_explicit(tmp_path):
    _write_gateway_yaml(tmp_path, "supervisor:\n  enabled: true\n")
    assert load_config(tmp_path).enabled is True


def test_custom_thresholds(tmp_path):
    yaml = (
        "supervisor:\n"
        "  notice_threshold_seconds:\n"
        "    claude: 45\n"
        "    codex: 120\n"
        "    default: 90\n"
    )
    _write_gateway_yaml(tmp_path, yaml)
    cfg = load_config(tmp_path)
    assert cfg.notice_threshold("claude") == 45
    assert cfg.notice_threshold("codex") == 120
    assert cfg.notice_threshold("pi") == 90   # falls back to default


def test_missing_supervisor_block(tmp_path):
    _write_gateway_yaml(tmp_path, "brains:\n  claude: {}\n")
    assert load_config(tmp_path).enabled is True  # defaults


def test_channel_disabled(tmp_path):
    yaml = "supervisor:\n  channels:\n    telegram: false\n    slack: true\n"
    _write_gateway_yaml(tmp_path, yaml)
    cfg = load_config(tmp_path)
    assert cfg.channel_enabled("telegram") is False
    assert cfg.channel_enabled("slack") is True


def test_narrator_brain_override(tmp_path):
    yaml = "supervisor:\n  narrator_brain: claude:haiku\n"
    _write_gateway_yaml(tmp_path, yaml)
    assert load_config(tmp_path).narrator_brain == "claude:haiku"


def test_recovery_config(tmp_path):
    yaml = (
        "supervisor:\n"
        "  recovery:\n"
        "    enabled: false\n"
        "    max_recovery_attempts: 5\n"
    )
    _write_gateway_yaml(tmp_path, yaml)
    cfg = load_config(tmp_path)
    assert cfg.recovery_enabled is False
    assert cfg.max_recovery_attempts == 5


def test_groups_config(tmp_path):
    yaml = "supervisor:\n  groups:\n    enabled: true\n    show_phase: true\n"
    _write_gateway_yaml(tmp_path, yaml)
    cfg = load_config(tmp_path)
    assert cfg.groups_enabled is True
    assert cfg.groups_show_phase is True
