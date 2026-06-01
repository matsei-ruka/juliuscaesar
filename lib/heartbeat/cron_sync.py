"""Heartbeat cron auto-sync.

Reads ``<instance>/heartbeat/tasks.yaml`` and reconciles the calling user's
crontab to match. Per-instance marker block, idempotent, preserves any
lines the operator added outside the block.

Public API:

    build_block(instance_dir, *, jc_binary=None, timezone=None) -> str
    read_current_crontab() -> str
    strip_block(crontab_text, instance_basename) -> str
    sync(instance_dir, *, dry_run=False) -> dict

See ``docs/specs/heartbeat-cron-sync.md``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .runner import _resolve_instance_timezone, load_tasks


BEGIN_MARKER = "# === JC-HEARTBEAT BEGIN instance="
END_MARKER = "# === JC-HEARTBEAT END instance="


def _block_re(basename: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?ms)^# === JC-HEARTBEAT BEGIN instance={re.escape(basename)} ===\n"
        rf".*?"
        rf"^# === JC-HEARTBEAT END instance={re.escape(basename)} ===\n?",
    )


_CRON_FIELD_RE = re.compile(r"^[\S]+$")


def _validate_schedule(expr: str) -> None:
    """Tiny sanity check on a cron expression. Five whitespace fields."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(
            f"schedule must be 5 whitespace-separated fields, got {len(fields)}: {expr!r}"
        )
    for f in fields:
        if not _CRON_FIELD_RE.match(f):
            raise ValueError(f"schedule contains an invalid field: {f!r}")


def _resolve_jc_binary() -> str:
    found = shutil.which("jc")
    if found:
        return found
    raise RuntimeError(
        "jc binary not on PATH — install JC shims (e.g. juliuscaesar/install.sh) "
        "before running `jc heartbeat cron sync`."
    )


def _ensure_log_dir(instance_dir: Path) -> Path:
    log_dir = instance_dir / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _collect_scheduled(tasks: dict) -> list[tuple[str, str]]:
    """Return (task_name, schedule) for every task with a usable schedule.

    Skips tasks where ``enabled`` is missing/false or where ``schedule`` is
    absent. Raises ValueError on a malformed schedule string so an operator
    typo doesn't silently desync.
    """
    out: list[tuple[str, str]] = []
    for name, task in (tasks or {}).items():
        if not isinstance(task, dict):
            continue
        if not task.get("enabled", False):
            continue
        schedule = task.get("schedule")
        if schedule in (None, ""):
            continue
        if not isinstance(schedule, str):
            raise ValueError(f"task {name}: schedule must be a string, got {type(schedule).__name__}")
        _validate_schedule(schedule)
        out.append((name, schedule))
    return out


def build_block(
    instance_dir: Path,
    *,
    jc_binary: str | None = None,
    timezone: str | None = None,
    tasks_override: dict | None = None,
) -> str:
    """Build the marker-wrapped crontab block for an instance.

    Returns "" if no tasks are scheduled (caller treats as block-absent).

    ``jc_binary`` and ``timezone`` are exposed for tests; production code
    leaves them as None so they resolve from PATH and gateway.yaml.
    """
    instance_dir = instance_dir.resolve()
    basename = instance_dir.name
    tasks_yaml = instance_dir / "heartbeat" / "tasks.yaml"
    if tasks_override is not None:
        tasks = tasks_override
    else:
        cfg = load_tasks(tasks_yaml)
        tasks = cfg.get("tasks") or {}
    scheduled = _collect_scheduled(tasks)
    if not scheduled:
        return ""

    jc_bin = jc_binary or _resolve_jc_binary()
    tz = timezone or _resolve_instance_timezone(instance_dir)
    log_path = instance_dir / "state" / "logs" / "heartbeat-cron.log"

    lines = [f"{BEGIN_MARKER}{basename} ==="]
    if tz:
        lines.append(f"CRON_TZ={tz}")
    for name, schedule in scheduled:
        lines.append(
            f"{schedule} {jc_bin} heartbeat run {name} "
            f"--instance-dir {instance_dir} >> {log_path} 2>&1"
        )
    lines.append(f"{END_MARKER}{basename} ===")
    return "\n".join(lines) + "\n"


def read_current_crontab() -> str:
    """Return the calling user's current crontab, or "" if none installed."""
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("crontab binary not found on PATH") from exc
    if result.returncode == 0:
        return result.stdout
    stderr_lc = (result.stderr or "").lower()
    if "no crontab" in stderr_lc or result.returncode == 1:
        return ""
    raise RuntimeError(
        f"crontab -l failed (rc={result.returncode}): {result.stderr.strip()}"
    )


def strip_block(crontab_text: str, instance_basename: str) -> str:
    """Remove any prior JC-HEARTBEAT block for this instance basename."""
    return _block_re(instance_basename).sub("", crontab_text)


def _install_crontab(text: str) -> None:
    proc = subprocess.run(
        ["crontab", "-"],
        input=text,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"crontab install failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )


def compose_crontab(prior: str, block: str, basename: str) -> str:
    """Return crontab text with the block for ``basename`` replaced."""
    stripped = strip_block(prior, basename)
    if stripped and not stripped.endswith("\n"):
        stripped += "\n"
    if not block:
        return stripped
    return f"{stripped}{block}"


def sync(
    instance_dir: Path,
    *,
    dry_run: bool = False,
    jc_binary: str | None = None,
    timezone: str | None = None,
    crontab_reader=None,
    crontab_writer=None,
) -> dict:
    """Reconcile the user's crontab with tasks.yaml.

    Returns a summary dict: ``{"basename", "scheduled", "block", "crontab",
    "installed"}``. ``installed`` is False on dry-run.

    ``crontab_reader``/``crontab_writer`` are test seams.
    """
    instance_dir = instance_dir.resolve()
    basename = instance_dir.name

    reader = crontab_reader or read_current_crontab
    writer = crontab_writer or _install_crontab

    _ensure_log_dir(instance_dir)
    block = build_block(
        instance_dir, jc_binary=jc_binary, timezone=timezone
    )
    prior = reader()
    new_text = compose_crontab(prior, block, basename)

    if not dry_run and new_text != prior:
        writer(new_text)

    scheduled_count = 0
    if block:
        # Count the actual command lines (block - markers - CRON_TZ).
        scheduled_count = sum(
            1
            for line in block.splitlines()
            if line
            and not line.startswith("#")
            and not line.startswith("CRON_TZ=")
        )

    return {
        "basename": basename,
        "scheduled": scheduled_count,
        "block": block,
        "crontab": new_text,
        "installed": (not dry_run) and (new_text != prior),
    }


def preview(instance_dir: Path) -> str:
    """Return the block that would be installed (without touching crontab)."""
    return build_block(instance_dir.resolve())
