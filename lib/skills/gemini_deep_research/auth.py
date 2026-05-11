"""Persistent Chromium profile + concurrency lock.

One profile per host (`$XDG_CONFIG_HOME/jc-skills/deep-research-profile/`)
shared by every instance on that host. This is intentional: Rachel and
Marco on the same VM drive the same Luca subscription.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import EXIT_BUSY, DeepResearchError


PROFILE_ENV_VAR = "JC_RESEARCH_PROFILE_DIR"
LOCK_FILENAME = ".jc-research.lock"
DEFAULT_LOCK_TIMEOUT = 60.0


def profile_dir() -> Path:
    """Return the per-host Chromium profile directory.

    Honours `JC_RESEARCH_PROFILE_DIR` so tests + alternate logins can point
    elsewhere. Falls back to `$XDG_CONFIG_HOME` then `~/.config`.
    """
    override = os.environ.get(PROFILE_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return (base / "jc-skills" / "deep-research-profile").resolve()


def ensure_profile_dir(*, create: bool = True) -> Path:
    path = profile_dir()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    enforce_permissions(path)
    return path


def enforce_permissions(path: Path) -> None:
    """Profile dirs hold cookies; lock them down to the operator user."""
    if not path.exists():
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def profile_exists() -> bool:
    p = profile_dir()
    return p.exists() and any(p.iterdir())


def profile_age_seconds() -> float | None:
    p = profile_dir()
    if not p.exists():
        return None
    try:
        return time.time() - p.stat().st_mtime
    except OSError:
        return None


def lock_path() -> Path:
    return profile_dir() / LOCK_FILENAME


@contextmanager
def acquire_lock(timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[None]:
    """Hold an exclusive `flock` on the profile lock file.

    Chromium itself locks `user-data-dir`; we lock the *same* directory so
    `jc research run` and `jc research start` queue cleanly instead of
    crashing Chromium with a stale-pid lock.
    """
    ensure_profile_dir()
    path = lock_path()
    deadline = time.monotonic() + max(0.0, timeout)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise DeepResearchError(
                        EXIT_BUSY,
                        "Another deep-research run is using the profile. "
                        "Wait or rerun with a longer --max-wait.",
                    )
                time.sleep(0.5)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


__all__ = [
    "PROFILE_ENV_VAR",
    "DEFAULT_LOCK_TIMEOUT",
    "profile_dir",
    "ensure_profile_dir",
    "enforce_permissions",
    "profile_exists",
    "profile_age_seconds",
    "lock_path",
    "acquire_lock",
]
