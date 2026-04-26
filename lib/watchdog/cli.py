"""`jc watchdog` v2 subcommands — status, tail, reset, reload, migrate."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from . import health, policy
from .child import ChildSpec, ChildState, StateStore, log_dir, state_dir
from .registry import (
    load_enabled,
    load_registry,
    registry_path,
    render_default_yaml,
)
from .supervisor import Supervisor


def cmd_status(instance_dir: Path, args: argparse.Namespace) -> int:
    store = StateStore(instance_dir)
    children = load_registry(instance_dir)
    rows: list[dict[str, Any]] = []
    now = time.time()
    for spec in children:
        st = store.get(spec.name)
        alive, reason = health.check(spec, st, instance_dir=instance_dir, now=now)
        mode = _mode_for(spec, st, alive)
        uptime = ""
        if alive and st.last_started_at:
            uptime = _format_duration(now - st.last_started_at)
        elif alive and st.last_pid:
            uptime = "(externally started)"
        restarts = len(st.attempts_in_window)
        rows.append(
            {
                "name": spec.name,
                "type": spec.type,
                "enabled": spec.enabled,
                "pid": st.last_pid if alive else None,
                "uptime": uptime,
                "restarts_in_window": restarts,
                "last_failure": "" if alive else (reason or st.last_failure),
                "mode": mode,
            }
        )
    if args.json:
        print(json.dumps({"instance_dir": str(instance_dir), "children": rows}, indent=2))
        return 0
    if not rows:
        print(
            f"no children found at {registry_path(instance_dir)}\n"
            f"run `jc watchdog migrate` to bootstrap from ops/watchdog.conf"
        )
        return 0
    name_w = max(8, max(len(r["name"]) for r in rows))
    fmt = f"{{:<{name_w}}}  {{:<7}}  {{:<8}}  {{:>9}}  {{:<32}}  {{:<10}}"
    print(fmt.format("NAME", "PID", "UPTIME", "RESTARTS", "LAST FAILURE", "MODE"))
    for row in rows:
        print(
            fmt.format(
                row["name"],
                str(row["pid"]) if row["pid"] is not None else "-",
                row["uptime"] or "-",
                row["restarts_in_window"],
                (row["last_failure"] or "-")[:32],
                row["mode"],
            )
        )
    return 0


def _mode_for(spec: ChildSpec, st: ChildState, alive: bool) -> str:
    if not spec.enabled:
        return "disabled"
    if st.alert_mode:
        return "alert"
    if alive:
        return "ok" + (" (legacy)" if spec.type == "legacy-claude" else "")
    if st.consecutive_failures > 0:
        return "backoff"
    return "down"


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h{minutes % 60:02d}m"


def cmd_tail(instance_dir: Path, args: argparse.Namespace) -> int:
    log_file = log_dir(instance_dir) / f"{args.child}.log"
    if not log_file.exists():
        print(f"no log file for child {args.child!r}: {log_file}")
        return 1
    text = log_file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    for line in lines[-args.lines :]:
        print(line)
    if args.follow:
        with log_file.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            while True:
                line = handle.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.5)
    return 0


def cmd_reset(instance_dir: Path, args: argparse.Namespace) -> int:
    store = StateStore(instance_dir)
    target = args.child
    children = {c.name for c in load_registry(instance_dir)}
    if target != "all" and target not in children:
        print(f"unknown child {target!r}; known: {', '.join(sorted(children)) or '(none)'}")
        return 2
    if target == "all":
        for name in children:
            store.reset(name)
    else:
        store.reset(target)
    store.save()
    print(f"reset state for {target}")
    return 0


def cmd_reload(instance_dir: Path, args: argparse.Namespace) -> int:
    """Re-trigger a tick under the lock — picks up any registry edits."""
    sup = Supervisor(instance_dir)
    return sup.run_tick()


def cmd_migrate(instance_dir: Path, args: argparse.Namespace) -> int:
    """Generate `ops/watchdog.yaml` from `ops/watchdog.conf`. Idempotent."""
    target = registry_path(instance_dir)
    if target.exists() and not args.force:
        print(f"refusing to overwrite {target} — pass --force to replace")
        return 1
    conf = instance_dir / "ops" / "watchdog.conf"
    runtime_mode = "gateway"
    screen_name = ""
    session_id = ""
    claude_extra = ""
    if conf.exists():
        for raw in conf.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip("'").strip('"')
            key = key.strip()
            if key == "RUNTIME_MODE":
                runtime_mode = value
            elif key == "SCREEN_NAME":
                screen_name = value
            elif key == "SESSION_ID":
                session_id = value
            elif key == "CLAUDE_ARGS_EXTRA":
                claude_extra = value
    if runtime_mode not in ("gateway", "legacy-claude"):
        print(f"unrecognized RUNTIME_MODE={runtime_mode!r}; aborting")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_migrated_yaml(runtime_mode, screen_name, session_id, claude_extra), encoding="utf-8")
    print(f"wrote {target} (runtime_mode={runtime_mode})")
    print("next: switch your cron entry to use `python3 -m watchdog.supervisor`,")
    print("      or rerun `jc watchdog install` once the v2 supervisor ships.")
    return 0


def _render_migrated_yaml(
    runtime_mode: str,
    screen_name: str,
    session_id: str,
    claude_extra: str,
) -> str:
    if runtime_mode == "gateway":
        return render_default_yaml()
    legacy_block = f"""# Watchdog v2 — generated by `jc watchdog migrate` from ops/watchdog.conf.
children:
  - name: claude-session
    type: legacy-claude
    enabled: true
    screen_name: {screen_name or 'jc-instance'}
    session_id: "{session_id}"
    health:
      cwd_match: $INSTANCE_DIR
      proc_match: "claude .*--channels plugin:telegram"
    restart:
      backoff: [5, 10, 30, 60, 300]
      max_in_window: 5
      window_seconds: 600
"""
    if claude_extra:
        legacy_block += f"    # CLAUDE_ARGS_EXTRA was set in watchdog.conf:\n    # {claude_extra}\n"
    return legacy_block


# --- parser plumbing ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jc watchdog v2")
    p.add_argument("--instance-dir", required=False)
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("status", help="Show child status table")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_status)

    pt = sub.add_parser("tail", help="Tail a child's per-child log")
    pt.add_argument("child")
    pt.add_argument("-n", "--lines", type=int, default=40)
    pt.add_argument("-f", "--follow", action="store_true")
    pt.set_defaults(func=cmd_tail)

    pr = sub.add_parser("reset", help="Clear alert mode + restart counters for a child")
    pr.add_argument("child", help="child name, or 'all'")
    pr.set_defaults(func=cmd_reset)

    prl = sub.add_parser("reload", help="Re-read registry and run one tick")
    prl.set_defaults(func=cmd_reload)

    pm = sub.add_parser("migrate", help="Generate ops/watchdog.yaml from ops/watchdog.conf")
    pm.add_argument("--force", action="store_true", help="Overwrite existing watchdog.yaml")
    pm.set_defaults(func=cmd_migrate)

    return p


def main(argv: list[str] | None = None, *, default_instance: Path | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    instance = _resolve_instance(args.instance_dir, default_instance)
    if instance is None:
        return 2
    return args.func(instance, args)


def _resolve_instance(arg: str | None, fallback: Path | None) -> Path | None:
    if arg:
        p = Path(arg).expanduser().resolve()
        if not p.is_dir():
            print(f"--instance-dir does not exist: {p}", file=sys.stderr)
            return None
        return p
    env = os.environ.get("JC_INSTANCE_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        if not p.is_dir():
            print(f"JC_INSTANCE_DIR does not exist: {p}", file=sys.stderr)
            return None
        return p
    if fallback is not None:
        return fallback.resolve()
    cur = Path.cwd().resolve()
    while True:
        if (cur / ".jc").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    print("Could not resolve instance dir. Use --instance-dir or set JC_INSTANCE_DIR.", file=sys.stderr)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
