"""Integration tests for jc-gateway start path env isolation.

Drives ``_build_child_env`` from the script so the wiring between
``--no-env-isolation`` and the sanitizer stays correct.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / "bin" / "jc-gateway"


def _load_gateway_module():
    loader = importlib.machinery.SourceFileLoader(
        "jc_gateway_bin_envtest", str(BIN)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def _make_instance(tmp_path: Path, env_lines: str) -> Path:
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / ".env").write_text(env_lines, encoding="utf-8")
    return instance


def test_build_child_env_strips_poison_when_dotenv_silent(monkeypatch, tmp_path):
    gateway = _load_gateway_module()
    instance = _make_instance(tmp_path, "DASHSCOPE_API_KEY=sk-real\n")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "POISON")
    monkeypatch.setenv("HOME", "/home/jc")
    monkeypatch.setenv("PATH", "/usr/bin")

    args = argparse.Namespace(no_env_isolation=False)
    env, audit = gateway._build_child_env(instance, args)

    assert env is not None
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert env["DASHSCOPE_API_KEY"] == "sk-real"
    assert env["HOME"] == "/home/jc"
    assert env["PATH"] == "/usr/bin"
    assert "stripped=" in audit
    assert "loaded=1" in audit


def test_build_child_env_dotenv_overrides_parent(monkeypatch, tmp_path):
    gateway = _load_gateway_module()
    instance = _make_instance(
        tmp_path, "TELEGRAM_BOT_TOKEN=CORRECT\n"
    )

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "POISON")
    args = argparse.Namespace(no_env_isolation=False)
    env, _audit = gateway._build_child_env(instance, args)

    assert env["TELEGRAM_BOT_TOKEN"] == "CORRECT"


def test_build_child_env_no_isolation_returns_none(monkeypatch, tmp_path):
    gateway = _load_gateway_module()
    instance = _make_instance(tmp_path, "")

    args = argparse.Namespace(no_env_isolation=True)
    env, audit = gateway._build_child_env(instance, args)

    assert env is None
    assert "skipped" in audit
    assert "--no-env-isolation" in audit
