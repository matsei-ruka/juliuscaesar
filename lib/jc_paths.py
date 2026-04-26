"""Shared path helpers for instance-local user configuration."""

from __future__ import annotations

import os
from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a configured path escapes the instance boundary."""


class InstanceResolutionError(ValueError):
    """Raised when an instance directory cannot be resolved."""


def resolve_instance_dir(
    arg: str | Path | None = None,
    *,
    fallback_markers: tuple[str | Path, ...] = ("memory",),
    cwd: Path | None = None,
) -> Path:
    """Resolve a JuliusCaesar instance directory.

    Resolution order is the public CLI contract:
    `--instance-dir` -> `$JC_INSTANCE_DIR` -> walk up for `.jc` -> cwd fallback
    when one of the caller-provided marker paths exists.
    """
    if arg:
        path = Path(arg).expanduser().resolve()
        if not path.exists():
            raise InstanceResolutionError(f"--instance-dir does not exist: {path}")
        return path

    env = os.environ.get("JC_INSTANCE_DIR")
    if env:
        path = Path(env).expanduser().resolve()
        if not path.exists():
            raise InstanceResolutionError(f"JC_INSTANCE_DIR does not exist: {path}")
        return path

    start = (cwd or Path.cwd()).resolve()
    cur = start
    while True:
        if (cur / ".jc").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent

    for marker in fallback_markers:
        if (start / marker).exists():
            return start

    raise InstanceResolutionError(
        "Could not resolve instance dir. Use --instance-dir, set JC_INSTANCE_DIR, "
        "or run from a directory containing a .jc marker or an instance marker."
    )


def resolve_instance_path(
    instance_dir: Path,
    value: str | Path,
    *,
    allowlist: list[str | Path] | tuple[str | Path, ...] = (),
) -> Path:
    instance = instance_dir.resolve()
    raw = os.path.expandvars(str(value)).replace("$INSTANCE_DIR", str(instance))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = instance / path
    resolved = path.resolve()
    if _inside(resolved, instance):
        return resolved
    for allowed in allowlist:
        allowed_path = Path(os.path.expandvars(str(allowed))).expanduser()
        if not allowed_path.is_absolute():
            allowed_path = instance / allowed_path
        if _inside(resolved, allowed_path.resolve()):
            return resolved
    raise UnsafePathError(f"path escapes instance: {value}")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
