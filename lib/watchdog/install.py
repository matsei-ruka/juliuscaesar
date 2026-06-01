"""Watchdog cron install/verify — marker-block convention.

Mirrors :mod:`lib.heartbeat.cron_sync` so the two blocks coexist in the
operator's crontab. See ``docs/specs/watchdog-self-install.md``.

Public API:

    build_block(instance_dir, *, jc_binary=None, tick_interval_minutes=2) -> str
    read_current_crontab() -> str
    strip_block(crontab_text, instance_basename) -> str
    compose_crontab(prior, block, basename) -> str
    install(instance_dir, *, dry_run=False, ...) -> dict
    verify(instance_dir, *, tick_interval_minutes=2, ...) -> Finding
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


BEGIN_MARKER = "# === JC-WATCHDOG BEGIN instance="
END_MARKER = "# === JC-WATCHDOG END instance="
LEGACY_TAG_PREFIX = "# jc-watchdog for "


@dataclass(frozen=True)
class Finding:
    level: str  # "ok" | "fail"
    message: str


def _block_re(basename: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?ms)^# === JC-WATCHDOG BEGIN instance={re.escape(basename)} ===\n"
        rf".*?"
        rf"^# === JC-WATCHDOG END instance={re.escape(basename)} ===\n?",
    )


def _resolve_jc_binary() -> str:
    found = shutil.which("jc")
    if found:
        return found
    raise RuntimeError(
        "jc binary not on PATH — install JC shims (e.g. juliuscaesar/install.sh) "
        "before running `jc watchdog install`."
    )


def build_block(
    instance_dir: Path,
    *,
    jc_binary: str | None = None,
    tick_interval_minutes: int = 2,
) -> str:
    if tick_interval_minutes < 1 or tick_interval_minutes > 59:
        raise ValueError(
            f"tick_interval_minutes must be in 1..59, got {tick_interval_minutes}"
        )
    instance_dir = instance_dir.resolve()
    basename = instance_dir.name
    jc_bin = jc_binary or _resolve_jc_binary()
    tick = f"*/{tick_interval_minutes} * * * *"
    reboot = "@reboot    "
    lines = [
        f"{BEGIN_MARKER}{basename} ===",
        f"{tick} {jc_bin} watchdog tick --instance-dir {instance_dir}",
        f"{reboot} {jc_bin} watchdog tick --instance-dir {instance_dir}",
        f"{END_MARKER}{basename} ===",
    ]
    return "\n".join(lines) + "\n"


def read_current_crontab() -> str:
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
    """Remove the marker block and any legacy `# jc-watchdog for <dir>` lines.

    Legacy tag lines from the pre-marker install are deleted only when they
    reference this instance's path (matched by basename ending). Other
    instances' legacy lines on the same host stay untouched.
    """
    text = _block_re(instance_basename).sub("", crontab_text)
    new_lines: list[str] = []
    legacy_suffix = f"/{instance_basename}"
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\n")
        if LEGACY_TAG_PREFIX in line and (
            line.rstrip().endswith(legacy_suffix)
            or f"{legacy_suffix} " in line
            or f"{legacy_suffix}\t" in line
        ):
            continue
        new_lines.append(raw)
    return "".join(new_lines)


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
    stripped = strip_block(prior, basename)
    if stripped and not stripped.endswith("\n"):
        stripped += "\n"
    if not block:
        return stripped
    return f"{stripped}{block}"


def install(
    instance_dir: Path,
    *,
    dry_run: bool = False,
    jc_binary: str | None = None,
    tick_interval_minutes: int = 2,
    crontab_reader=None,
    crontab_writer=None,
) -> dict:
    instance_dir = instance_dir.resolve()
    basename = instance_dir.name
    reader = crontab_reader or read_current_crontab
    writer = crontab_writer or _install_crontab

    block = build_block(
        instance_dir,
        jc_binary=jc_binary,
        tick_interval_minutes=tick_interval_minutes,
    )
    prior = reader()
    new_text = compose_crontab(prior, block, basename)

    if not dry_run and new_text != prior:
        writer(new_text)

    return {
        "basename": basename,
        "block": block,
        "crontab": new_text,
        "installed": (not dry_run) and (new_text != prior),
    }


def verify(
    instance_dir: Path,
    *,
    tick_interval_minutes: int = 2,
    crontab_reader=None,
) -> Finding:
    instance_dir = instance_dir.resolve()
    basename = instance_dir.name
    reader = crontab_reader or read_current_crontab
    try:
        text = reader()
    except RuntimeError as exc:
        return Finding("fail", f"watchdog cron unreadable: {exc}")

    match = _block_re(basename).search(text)
    if not match:
        return Finding(
            "fail",
            f"watchdog cron block missing (instance={basename}) — "
            f"run: jc watchdog install",
        )
    block = match.group(0)
    expected_tick = f"*/{tick_interval_minutes} "
    has_tick = any(
        line.startswith(expected_tick)
        for line in block.splitlines()
    )
    has_reboot = any(
        line.lstrip().startswith("@reboot") for line in block.splitlines()
    )
    if not has_tick:
        return Finding(
            "fail",
            f"watchdog cron block present but tick cadence does not match "
            f"*/{tick_interval_minutes} (instance={basename})",
        )
    if not has_reboot:
        return Finding(
            "fail",
            f"watchdog cron block present but @reboot line is missing "
            f"(instance={basename})",
        )
    return Finding("ok", f"watchdog cron block present (instance={basename})")
