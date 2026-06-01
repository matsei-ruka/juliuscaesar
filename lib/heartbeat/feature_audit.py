"""Post-update feature audit.

Scans an instance for the on/off state of every opt-in capability we ship,
and (via the companion ``jc-features`` binary) notifies the operator about
features they haven't turned on. Designed to be invoked from a release
hook so each upgrade surfaces what just became available.

See ``docs/specs/feature-audit-notifier.md``.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import yaml


SNAPSHOT_PATH = "state/feature-audit-snapshot.json"

Status = Literal["enabled", "disabled", "missing"]


@dataclass(frozen=True)
class Feature:
    name: str
    status: Status
    where: str
    hint: str


# Hard-coded feature table. Adding a feature = one line here.
BUILTINS: tuple[tuple[str, str], ...] = (
    ("dream_tick", "Nightly offline-reflection cycle"),
    ("self_model_run", "Autonomous self-observation loop"),
    ("hot_tidy", "Trim HOT.md to size caps, archive overflow to L2"),
    ("journal_tidy", "Archive resolved threads from journal to L2"),
    ("commitments_tick", "Fire due deferred commitments"),
    ("reengage_tick", "Queue re-engagement on chat silence"),
)


GATEWAY_FEATURES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("actions", ("actions", "enabled"),
     "Supervisor card Stop / Background buttons"),
    ("accountabilities", ("accountabilities", "enabled"),
     "Accountabilities tracking + enactment tokens"),
    ("entities", ("entities", "enabled"),
     "Typed entity store + people migration"),
    ("inter-agent-protocol", ("inter_agent_protocol", "enabled"),
     "Inter-agent protocol with authority-map handshake"),
    ("adaptive-discovery", ("adaptive_discovery", "enabled"),
     "Adaptive discovery posture for unknown peers"),
    ("voice-channel", ("channels", "voice", "enabled"),
     "Voice channel (ASR + TTS over Telegram)"),
    ("email-channel", ("channels", "email", "enabled"),
     "Email channel ingestion + reply"),
)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def _dig(data: dict, path: tuple[str, ...]) -> object | None:
    cur: object = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def _crontab_has_task(crontab_text: str, instance_basename: str, task_name: str) -> bool:
    """Check that the JC-HEARTBEAT block for this instance schedules ``task_name``.

    Matches the runner command at the end of a non-comment line. Catches the
    ``enabled: true but no cron line`` failure mode that bit Mikaela.
    """
    if not crontab_text:
        return False
    block_re = re.compile(
        rf"(?ms)^# === JC-HEARTBEAT BEGIN instance={re.escape(instance_basename)} ===\n"
        rf"(.*?)"
        rf"^# === JC-HEARTBEAT END instance={re.escape(instance_basename)} ===",
    )
    m = block_re.search(crontab_text)
    if not m:
        return False
    body = m.group(1)
    pattern = re.compile(
        rf"^[^#\n]*\bheartbeat run {re.escape(task_name)}\b",
        re.MULTILINE,
    )
    return bool(pattern.search(body))


def _read_crontab() -> str:
    """Best-effort read of the calling user's crontab.

    Returns "" on any failure (no crontab installed, no binary, etc.). The
    audit is read-only and must not blow up the upgrade hook just because
    cron isn't available on a headless runner.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _audit_builtins(
    instance_dir: Path,
    tasks: dict,
    crontab_text: str,
) -> list[Feature]:
    basename = instance_dir.name
    out: list[Feature] = []
    for name, hint in BUILTINS:
        if name not in tasks:
            out.append(
                Feature(
                    name=name,
                    status="missing",
                    where=f"heartbeat/tasks.yaml:tasks.{name}",
                    hint=hint,
                )
            )
            continue
        task = tasks[name] or {}
        enabled_flag = bool(task.get("enabled", False))
        has_cron = _crontab_has_task(crontab_text, basename, name)
        status: Status = "enabled" if (enabled_flag and has_cron) else "disabled"
        if enabled_flag and not has_cron:
            where = (
                f"heartbeat/tasks.yaml:tasks.{name} (enabled, no cron line — "
                f"run `jc heartbeat cron sync`)"
            )
        else:
            where = f"heartbeat/tasks.yaml:tasks.{name}"
        out.append(Feature(name=name, status=status, where=where, hint=hint))
    return out


def _audit_gateway(instance_dir: Path) -> list[Feature]:
    cfg = _load_yaml(instance_dir / "ops" / "gateway.yaml")
    out: list[Feature] = []
    for name, key_path, hint in GATEWAY_FEATURES:
        val = _dig(cfg, key_path)
        if val is None:
            status: Status = "disabled"
        else:
            status = "enabled" if bool(val) else "disabled"
        where = "ops/gateway.yaml:" + ".".join(key_path)
        out.append(Feature(name=name, status=status, where=where, hint=hint))
    return out


def scan(instance_dir: Path, *, crontab_reader=None) -> list[Feature]:
    """Return one Feature per opt-in capability, in stable order.

    ``crontab_reader`` is a test seam — production callers leave it as None.
    """
    instance_dir = instance_dir.resolve()
    tasks_yaml = instance_dir / "heartbeat" / "tasks.yaml"
    tasks_cfg = _load_yaml(tasks_yaml)
    tasks = (tasks_cfg.get("tasks") or {}) if isinstance(tasks_cfg, dict) else {}
    crontab_text = (crontab_reader or _read_crontab)()
    return _audit_builtins(instance_dir, tasks, crontab_text) + _audit_gateway(instance_dir)


def load_snapshot(instance_dir: Path) -> dict:
    path = instance_dir / SNAPSHOT_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_snapshot(instance_dir: Path, features: list[Feature], *, ts: str | None = None) -> Path:
    from datetime import datetime, timezone

    path = instance_dir / SNAPSHOT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": ts or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "features": {f.name: f.status for f in features},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def diff_new(features: list[Feature], snapshot: dict) -> list[Feature]:
    """Return features absent from the prior snapshot.

    First-appearance semantics — flipping ``enabled``→``disabled`` is the
    operator's call, not a discovery moment.
    """
    known = set((snapshot.get("features") or {}).keys())
    return [f for f in features if f.name not in known]


def build_telegram_message(features: list[Feature], *, only_new: bool) -> str | None:
    """Return the MarkdownV2-safe message body, or None if nothing to say."""
    if only_new:
        if not features:
            return None
        heading = f"*New JC features available* — {len(features)}"
    else:
        disabled = [f for f in features if f.status == "disabled"]
        if not disabled:
            return None
        features = disabled
        heading = f"*Disabled JC features* — {len(features)}"

    lines = [heading, ""]
    for f in features:
        lines.append(f"• `{f.name}` — {f.hint}")
    lines.append("")
    lines.append("Reply with the feature name to enable, or `skip` to dismiss.")
    return "\n".join(lines)


def features_to_dicts(features: list[Feature]) -> list[dict]:
    return [asdict(f) for f in features]
