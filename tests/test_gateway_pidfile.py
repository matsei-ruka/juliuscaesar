from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / "bin" / "jc-gateway"


def _load_gateway_module():
    loader = importlib.machinery.SourceFileLoader("jc_gateway_bin", str(BIN))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def _make_instance(tmp_path: Path) -> Path:
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / ".jc").write_text("", encoding="utf-8")
    (instance / "ops").mkdir(parents=True)
    (instance / "memory").mkdir()
    (instance / "ops" / "gateway.yaml").write_text(
        "default_brain: claude\nchannels: {}\n",
        encoding="utf-8",
    )
    return instance


def test_start_removes_live_foreign_pidfile_and_launches_gateway(monkeypatch, tmp_path):
    gateway = _load_gateway_module()
    instance = _make_instance(tmp_path)
    foreign_pid = 780582
    gateway.pid_path(instance).parent.mkdir(parents=True)
    gateway.pid_path(instance).write_text(f"{foreign_pid}\n", encoding="utf-8")

    monkeypatch.setattr(gateway, "resolve_instance_dir", lambda _arg: instance.resolve())
    monkeypatch.setattr(gateway, "validate_config", lambda _instance: None)
    class FakeConn:
        def close(self):
            return None

    monkeypatch.setattr(gateway.queue, "connect", lambda _instance: FakeConn())
    monkeypatch.setattr(gateway, "pid_alive", lambda pid: pid == foreign_pid or pid == 12345)
    monkeypatch.setattr(gateway, "proc_cwd", lambda _pid: Path("/root"))
    monkeypatch.setattr(gateway, "proc_cmdline", lambda _pid: "bash")

    class FakePopen:
        pid = 12345

        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.kwargs = kwargs

    popens = []

    def fake_popen(cmd, **kwargs):
        proc = FakePopen(cmd, **kwargs)
        popens.append(proc)
        return proc

    monkeypatch.setattr(gateway.subprocess, "Popen", fake_popen)

    rc = gateway.cmd_start(
        argparse.Namespace(instance_dir=str(instance), interval_seconds=5.0)
    )

    assert rc == 0
    assert popens
    assert popens[0].kwargs["cwd"] == str(instance.resolve())
    assert gateway.pid_path(instance).read_text(encoding="utf-8") == "12345\n"


def test_stop_does_not_kill_live_foreign_pidfile(monkeypatch, tmp_path, capsys):
    gateway = _load_gateway_module()
    instance = _make_instance(tmp_path)
    foreign_pid = 780582
    gateway.pid_path(instance).parent.mkdir(parents=True)
    gateway.pid_path(instance).write_text(f"{foreign_pid}\n", encoding="utf-8")

    killed = []
    monkeypatch.setattr(gateway, "resolve_instance_dir", lambda _arg: instance.resolve())
    monkeypatch.setattr(gateway, "pid_alive", lambda pid: pid == foreign_pid)
    monkeypatch.setattr(gateway, "proc_cwd", lambda _pid: Path("/root"))
    monkeypatch.setattr(gateway, "proc_cmdline", lambda _pid: "bash")
    monkeypatch.setattr(gateway.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    rc = gateway.cmd_stop(
        argparse.Namespace(instance_dir=str(instance), timeout_seconds=0.1, kill=False)
    )

    assert rc == 0
    assert killed == []
    assert not gateway.pid_path(instance).exists()
    assert "removed foreign pidfile" in capsys.readouterr().out
