"""Release hook for 2026.05.02 gateway bootstrap."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from gateway import config as gw_config
from jc_paths import InstanceResolutionError, resolve_instance_dir as _resolve_instance_dir


RELEASE_VERSION = "2026.05.02"


def resolve_instance_dir(arg: str | None) -> Path | None:
    calling_cwd = os.environ.get("JC_UPDATE_CALLING_CWD")
    cwd = Path(calling_cwd).expanduser().resolve() if calling_cwd else None
    try:
        return _resolve_instance_dir(arg, fallback_markers=("memory",), cwd=cwd)
    except InstanceResolutionError as exc:
        if arg or os.environ.get("JC_INSTANCE_DIR"):
            raise SystemExit(str(exc)) from exc
        return None


def _parse_env_like(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            out[key] = value
    return out


def _set_runtime_mode(path: Path, mode: str, *, dry_run: bool) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    new = re.sub(
        r"^RUNTIME_MODE\s*=.*$",
        f"RUNTIME_MODE={mode}",
        text,
        flags=re.MULTILINE,
    )
    if new == text and "RUNTIME_MODE=" not in text:
        new = text.rstrip() + f"\nRUNTIME_MODE={mode}\n"
    if new == text:
        return False
    if not dry_run:
        path.write_text(new, encoding="utf-8")
    return True


def apply(instance: Path, *, dry_run: bool) -> int:
    watchdog_path = instance / "ops" / "watchdog.conf"
    gateway_path = instance / "ops" / "gateway.yaml"
    env_path = instance / ".env"

    watchdog = _parse_env_like(watchdog_path)
    env = _parse_env_like(env_path)
    chat_id = watchdog.get("TELEGRAM_CHAT_ID", "")
    telegram_enabled = bool(chat_id) or "TELEGRAM_BOT_TOKEN" in env

    actions: list[str] = []
    if gateway_path.exists():
        actions.append("ops/gateway.yaml already exists; left unchanged")
    else:
        new_yaml = gw_config.render_default_config(
            default_brain="claude",
            telegram_enabled=telegram_enabled,
            telegram_chat_id=chat_id,
            slack_enabled=False,
            discord_enabled=False,
            triage_backend="none",
        )
        actions.append("created ops/gateway.yaml")
        if not dry_run:
            gateway_path.parent.mkdir(parents=True, exist_ok=True)
            gateway_path.write_text(new_yaml, encoding="utf-8")

    if watchdog_path.exists():
        backup = watchdog_path.with_suffix(
            f".conf.bak.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        if not dry_run:
            backup.write_text(watchdog_path.read_text(encoding="utf-8"), encoding="utf-8")
        actions.append(f"backed up watchdog.conf -> {backup.name}")
        if _set_runtime_mode(watchdog_path, "gateway", dry_run=dry_run):
            actions.append("set RUNTIME_MODE=gateway in watchdog.conf")
    else:
        actions.append("watchdog.conf missing; skipped runtime mode flip")

    print(f"release_update={RELEASE_VERSION}")
    print(f"instance={instance}")
    for action in actions:
        print(f"- {action}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"release-update-{RELEASE_VERSION}")
    parser.add_argument("--from-version", default="")
    parser.add_argument("--to-version", default=RELEASE_VERSION)
    parser.add_argument("--instance-dir")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    instance = resolve_instance_dir(args.instance_dir)
    if instance is None:
        print(f"release_update={RELEASE_VERSION}")
        print("instance_resolved=false")
        print("release hook complete; no instance directory was available")
        return 0
    return apply(instance, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

