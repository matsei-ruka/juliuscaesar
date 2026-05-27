"""Reporter runtime introspection helpers.

Spec: ``docs/specs/reporter-runtime-snapshot.md`` §3.

Each helper is best-effort: introspection failures return ``None`` rather
than raising. The reporter assembles a ``runtime`` block from these
values and includes it in every ``gateway.snapshot`` payload. The
backend then merges that block into ``agent.deployment.runtime`` so the
dashboard reflects observation, not the value typed at register.

All helpers are pure functions — no module-level side effects, no
caching — so unit tests can monkeypatch ``socket.*`` cleanly. The
reporter itself owns the process ``start_time`` (recorded at Reporter
``__init__``) and passes it to :func:`uptime_seconds`.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit


def hostname() -> Optional[str]:
    """Return ``socket.gethostname()`` or ``None`` if it raises.

    Failure mode (spec §6.1): the hostname lookup itself can raise on a
    broken NSS / glibc — extremely rare but not impossible. Snapshot
    still publishes; this field just becomes ``None``.
    """
    try:
        name = socket.gethostname()
    except Exception:  # noqa: BLE001
        return None
    return name or None


def primary_ip(endpoint_url: str) -> Optional[str]:
    """Source IP that would reach ``endpoint_url`` (spec §3.2).

    UDP-socket-bind-to-the-endpoint-without-sending trick: ``connect``
    on a UDP socket only sets the kernel's routing table lookup; no
    packet leaves the host. ``getsockname()`` then reveals the
    interface IP the kernel chose.

    Falls back to ``gethostbyname(gethostname())`` if the UDP trick
    raises (e.g. unresolved endpoint hostname, no route). Returns
    ``None`` if both fail — the snapshot still publishes.

    Failure modes named: no network at all → ``None``; endpoint host
    portion missing → ``None``.
    """
    host = _host_from_endpoint(endpoint_url)
    if host:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # Port doesn't matter — only the routing lookup does.
                s.connect((host, 80))
                ip = s.getsockname()[0]
                if ip:
                    return str(ip)
            finally:
                s.close()
        except Exception:  # noqa: BLE001
            pass

    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip:
            return str(ip)
    except Exception:  # noqa: BLE001
        pass
    return None


def framework_commit(version_string: Optional[str]) -> Optional[str]:
    """Parse the git SHA portion out of a ``framework_version()`` string.

    Today ``lib.company.conf.framework_version()`` returns shapes like:

    - ``2026.05.27.01``                 — no git repo
    - ``2026.05.27.01+73bf58d``         — clean checkout
    - ``2026.05.27.01+73bf58d-dirty``   — uncommitted changes

    We split on ``+`` and strip the optional ``-dirty`` suffix. If
    there's no ``+`` the parse fails → ``None`` (spec §3.3).
    """
    if not version_string or "+" not in version_string:
        return None
    suffix = version_string.split("+", 1)[1]
    sha = suffix.split("-", 1)[0].strip()
    return sha or None


def supervisor_pid(instance_dir: Path) -> Optional[int]:
    """Read ``<instance_dir>/state/supervisor/jc-supervisor.pid``.

    Returns the integer PID, or ``None`` if the file is missing /
    unreadable / non-integer. The reporter does not try to discover
    the supervisor any other way — spec §3.4.
    """
    pid_path = Path(instance_dir) / "state" / "supervisor" / "jc-supervisor.pid"
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def uptime_seconds(start_time: float) -> int:
    """Seconds elapsed since ``start_time`` (a ``time.time()`` snapshot).

    Caller is responsible for capturing ``start_time`` at the right
    boundary (Reporter ``__init__`` today). Always returns an int —
    fractional seconds are noise for the dashboard.
    """
    return int(time.time() - start_time)


def _host_from_endpoint(endpoint_url: str) -> Optional[str]:
    """Extract just the host portion from ``http(s)://host[:port]/...``.

    Returns ``None`` if the URL has no recognisable host — keeps the
    UDP-trick branch from raising on a malformed configuration.
    """
    if not endpoint_url:
        return None
    try:
        parts = urlsplit(endpoint_url)
    except ValueError:
        return None
    host = parts.hostname
    return host or None
