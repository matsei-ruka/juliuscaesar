"""Config loader for the supervisor-driven the-company worker reporter.

Reads ``<instance_dir>/ops/the_company.yaml``:

    the_company:
      enabled: true
      api_url: http://192.168.14.112:8080
      agent_id: 4651933b-8ecd-4e23-992a-5e7cf56aafac
      api_key_file: state/company/api-key

If the file is missing, the top-level ``the_company:`` key is absent, or
``enabled`` is false, returns a ``CompanyConfig`` with ``disabled=True``.

This loader is intentionally separate from ``lib/company/conf.py`` (which
configures the older fleet client backed by ``requests``). The supervisor
reporter uses urllib-only with no new pip deps.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from watchdog.registry import _parse_yaml


@dataclass(frozen=True)
class CompanyConfig:
    """Frozen view of the_company.yaml.

    When ``disabled`` is True every other field is unused and the supervisor
    skips reporting entirely.
    """

    disabled: bool = True
    api_url: str = ""
    agent_id: str = ""
    api_key: str = ""

    @property
    def enabled(self) -> bool:
        return not self.disabled


def load(instance_dir: Path) -> CompanyConfig:
    """Load and validate the the_company block.

    Failure modes (all return ``CompanyConfig(disabled=True)``):
    - file missing
    - YAML parse error
    - ``the_company:`` block missing or not a dict
    - ``enabled: false``
    - ``api_url`` empty
    - ``api_key_file`` missing, unreadable, or empty after strip

    Note: this is a best-effort loader. It never raises. The supervisor's
    tick must keep running even if config is broken.
    """
    path = Path(instance_dir) / "ops" / "the_company.yaml"
    if not path.exists():
        return CompanyConfig()
    try:
        data = _parse_yaml(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return CompanyConfig()
    raw = data.get("the_company") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return CompanyConfig()

    if not _bool(raw.get("enabled"), False):
        return CompanyConfig()

    api_url = str(raw.get("api_url") or "").strip().rstrip("/")
    agent_id = str(raw.get("agent_id") or "").strip()
    api_key_file = str(raw.get("api_key_file") or "").strip()

    if not api_url:
        return CompanyConfig()

    api_key = _read_key(instance_dir, api_key_file)
    if not api_key:
        return CompanyConfig()

    return CompanyConfig(
        disabled=False,
        api_url=api_url,
        agent_id=agent_id,
        api_key=api_key,
    )


def _read_key(instance_dir: Path, api_key_file: str) -> str:
    if not api_key_file:
        return ""
    key_path = Path(api_key_file)
    if not key_path.is_absolute():
        key_path = Path(instance_dir) / api_key_file
    try:
        return key_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
