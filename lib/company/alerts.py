"""Convenience entrypoint for raising alerts from anywhere in JC.

Standalone fn that builds a one-shot client (no reporter required) so a
worker process or CLI can call it without going through the gateway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .client import CompanyClient, CompanyError
from .conf import load


SUPPORTED_SEVERITIES = ("info", "warn", "error", "critical")


def raise_alert(
    instance_dir: Path,
    *,
    title: str,
    severity: str = "info",
    body: str = "",
    link: str = "",
) -> Optional[dict[str, Any]]:
    """POST an alert. Returns ``{alert_id, share_url}`` on success, else ``None``.

    Never raises — alerts must be best-effort. ``severity`` must be one of
    ``info | warn | error | critical``.
    """
    if severity not in SUPPORTED_SEVERITIES:
        raise ValueError(f"severity must be one of {SUPPORTED_SEVERITIES}, got {severity!r}")

    cfg = load(Path(instance_dir))
    if not cfg.endpoint or not cfg.api_key:
        return None

    client = CompanyClient(cfg)
    try:
        return client.post_alert(
            {
                "severity": severity,
                "title": title,
                "body": body or None,
                "link": link or None,
            }
        )
    except CompanyError:
        return None
    finally:
        client.close()
