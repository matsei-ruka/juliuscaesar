"""Tests for jc-gateway orphan-process detection and reaping.

When a gateway is restarted (drift exit, manual restart, supervisor respawn)
the previous PID is normally pidfile-tracked and SIGTERMed. But foreign
pidfiles, fast-spawn races, or out-of-band launches can leave duplicates
that aren't tracked anywhere — these are observed across the fleet polling
the same Telegram bot and trigger 409 Conflict.

`find_gateway_pids_for_instance` walks /proc to surface every match;
`reap_orphan_gateways` SIGTERMs (then SIGKILLs) everything except the
canonical PID. `cmd_start` invokes the sweep before deciding whether to
spawn — so reboots converge on a single canonical process per instance.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import signal
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


def _planted_proc_table(instance: Path, entries: dict[int, str]) -> dict[str, callable]:
    """Build os.listdir + proc_cmdline replacements that pretend `entries` exist."""
    target = str(instance.resolve())

    def fake_listdir(path):
        if path == "/proc":
            return [str(pid) for pid in entries]
        raise OSError(2, "No such file or directory")

    def fake_cmdline(pid):
        return entries.get(pid, "")

    # Drop a defensive reference to target so the fixture self-documents
    assert target  # noqa: S101
    return {"listdir": fake_listdir, "cmdline": fake_cmdline}


def test_find_gateway_pids_matches_only_run_for_target_instance(monkeypatch, tmp_path):
    gw = _load_gateway_module()
    instance = _make_instance(tmp_path)
    target = str(instance.resolve())
    other = "/some/other/instance"
    table = {
        101: f"python3 /usr/local/bin/jc-gateway --instance-dir {target} run --interval-seconds 1.0",
        102: f"python3 /usr/local/bin/jc-gateway --instance-dir {other} run --interval-seconds 1.0",
        103: f"python3 /usr/local/bin/jc-gateway --instance-dir {target} status",  # not "run"
        104: "bash -c 'cat /tmp/foo'",
        105: f"python3 - {target} 5",  # supervisor watcher, not gateway
        106: f"/opt/jc-gateway --instance-dir {target} run",  # second canonical-shaped
    }
    fakes = _planted_proc_table(instance, table)
    monkeypatch.setattr(gw.os, "listdir", fakes["listdir"])
    monkeypatch.setattr(gw, "proc_cmdline", fakes["cmdline"])

    found = sorted(gw.find_gateway_pids_for_instance(instance))
    assert found == [101, 106]


def test_reap_orphan_gateways_skips_keep_pid_and_kills_rest(monkeypatch, tmp_path):
    gw = _load_gateway_module()
    instance = _make_instance(tmp_path)
    target = str(instance.resolve())
    table = {
        201: f"python3 /usr/local/bin/jc-gateway --instance-dir {target} run",
        202: f"python3 /usr/local/bin/jc-gateway --instance-dir {target} run --interval-seconds 5.0",
        203: f"python3 /usr/local/bin/jc-gateway --instance-dir {target} run --interval-seconds 1.0",
    }
    fakes = _planted_proc_table(instance, table)
    monkeypatch.setattr(gw.os, "listdir", fakes["listdir"])
    monkeypatch.setattr(gw, "proc_cmdline", fakes["cmdline"])

    sent_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(gw.os, "kill", lambda pid, sig: sent_signals.append((pid, sig)))
    # Pretend processes die after SIGTERM — pid_alive returns False
    monkeypatch.setattr(gw, "pid_alive", lambda pid: False)
    # Skip the 5s sleep
    monkeypatch.setattr(gw.time, "sleep", lambda _s: None)
    monkeypatch.setattr(gw.time, "monotonic", lambda: 0.0)

    reaped = gw.reap_orphan_gateways(instance, keep_pid=203)
    assert sorted(reaped) == [201, 202]
    # SIGTERM only — processes died before the SIGKILL fallback
    assert sorted(sent_signals) == sorted([(201, signal.SIGTERM), (202, signal.SIGTERM)])


def test_reap_orphan_gateways_escalates_to_sigkill_when_term_ignored(monkeypatch, tmp_path):
    gw = _load_gateway_module()
    instance = _make_instance(tmp_path)
    target = str(instance.resolve())
    table = {301: f"jc-gateway --instance-dir {target} run"}
    fakes = _planted_proc_table(instance, table)
    monkeypatch.setattr(gw.os, "listdir", fakes["listdir"])
    monkeypatch.setattr(gw, "proc_cmdline", fakes["cmdline"])

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(gw.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(gw, "pid_alive", lambda pid: True)  # never dies on TERM

    # Drive time so the 5s deadline expires immediately
    times = iter([0.0, 0.5, 10.0])
    monkeypatch.setattr(gw.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(gw.time, "sleep", lambda _s: None)

    reaped = gw.reap_orphan_gateways(instance, keep_pid=None)
    assert reaped == [301]
    assert (301, signal.SIGTERM) in sent
    assert (301, signal.SIGKILL) in sent


def test_cmd_start_reaps_orphans_before_spawn(monkeypatch, tmp_path):
    gw = _load_gateway_module()
    instance = _make_instance(tmp_path)
    target = str(instance.resolve())
    # Stale pidfile pointing at a process that no longer exists
    gw.pid_path(instance).parent.mkdir(parents=True)
    gw.pid_path(instance).write_text("9999\n", encoding="utf-8")

    # Two orphan gateways for this instance still alive
    table = {
        4001: f"jc-gateway --instance-dir {target} run --interval-seconds 5.0",
        4002: f"jc-gateway --instance-dir {target} run --interval-seconds 5.0",
    }

    monkeypatch.setattr(gw, "resolve_instance_dir", lambda _arg: instance.resolve())
    monkeypatch.setattr(gw, "validate_config", lambda _instance: None)

    class FakeConn:
        def close(self):
            return None

    monkeypatch.setattr(gw.queue, "connect", lambda _instance: FakeConn())
    monkeypatch.setattr(gw.os, "listdir", lambda path: [str(p) for p in table] if path == "/proc" else [])
    monkeypatch.setattr(gw, "proc_cmdline", lambda pid: table.get(pid, ""))
    # Stale pidfile PID is dead; orphans are alive only until SIGTERM
    alive_state = {4001: True, 4002: True, 9999: False}

    def fake_pid_alive(pid):
        return bool(alive_state.get(pid, False))

    monkeypatch.setattr(gw, "pid_alive", fake_pid_alive)
    monkeypatch.setattr(gw, "proc_cwd", lambda _pid: None)

    killed: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        if sig == signal.SIGTERM:
            alive_state[pid] = False

    monkeypatch.setattr(gw.os, "kill", fake_kill)
    monkeypatch.setattr(gw.time, "sleep", lambda _s: None)
    monkeypatch.setattr(gw.time, "monotonic", lambda: 0.0)

    class FakePopen:
        pid = 5555

        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.kwargs = kwargs

    spawned: list[FakePopen] = []

    def fake_popen(cmd, **kwargs):
        proc = FakePopen(cmd, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(gw.subprocess, "Popen", fake_popen)

    rc = gw.cmd_start(argparse.Namespace(instance_dir=str(instance), interval_seconds=1.0))

    assert rc == 0
    # Both orphans got SIGTERM
    assert (4001, signal.SIGTERM) in killed
    assert (4002, signal.SIGTERM) in killed
    # Fresh spawn happened (orphans cleared the path)
    assert spawned and spawned[0].pid == 5555
    # Pidfile now points at the fresh PID
    assert gw.pid_path(instance).read_text(encoding="utf-8") == "5555\n"


def test_cmd_start_keeps_canonical_and_reaps_only_extras(monkeypatch, tmp_path):
    gw = _load_gateway_module()
    instance = _make_instance(tmp_path)
    target = str(instance.resolve())
    canonical_pid = 7777
    gw.pid_path(instance).parent.mkdir(parents=True)
    gw.pid_path(instance).write_text(f"{canonical_pid}\n", encoding="utf-8")
    table = {
        canonical_pid: f"jc-gateway --instance-dir {target} run --interval-seconds 1.0",
        4444: f"jc-gateway --instance-dir {target} run --interval-seconds 5.0",
    }

    monkeypatch.setattr(gw, "resolve_instance_dir", lambda _arg: instance.resolve())
    monkeypatch.setattr(gw, "validate_config", lambda _instance: None)

    class FakeConn:
        def close(self):
            return None

    monkeypatch.setattr(gw.queue, "connect", lambda _instance: FakeConn())
    monkeypatch.setattr(gw.os, "listdir", lambda path: [str(p) for p in table] if path == "/proc" else [])
    monkeypatch.setattr(gw, "proc_cmdline", lambda pid: table.get(pid, ""))
    alive_pids = set(table.keys())
    monkeypatch.setattr(gw, "pid_alive", lambda pid: pid in alive_pids)
    monkeypatch.setattr(gw, "proc_cwd", lambda _pid: instance.resolve())

    killed: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        if sig == signal.SIGTERM:
            alive_pids.discard(pid)

    monkeypatch.setattr(gw.os, "kill", fake_kill)
    monkeypatch.setattr(gw.time, "sleep", lambda _s: None)
    monkeypatch.setattr(gw.time, "monotonic", lambda: 0.0)

    spawned: list[object] = []
    monkeypatch.setattr(gw.subprocess, "Popen", lambda *a, **kw: spawned.append((a, kw)) or object())

    rc = gw.cmd_start(argparse.Namespace(instance_dir=str(instance), interval_seconds=1.0))

    assert rc == 0
    # Canonical preserved; only the 5s orphan got signalled
    assert canonical_pid not in [pid for pid, _sig in killed]
    assert (4444, signal.SIGTERM) in killed
    # No fresh spawn — canonical already running
    assert spawned == []
