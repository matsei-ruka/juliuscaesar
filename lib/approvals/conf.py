"""Loader for `ops/approvals.yaml` runtime knobs (expiry, notify defaults, retention)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import ApprovalKind


DEFAULT_EXPIRES_IN_HOURS: dict[str, int] = {
    "default": 72,
    ApprovalKind.SENDER_AUTHORIZE.value: 168,
    ApprovalKind.GROUP_AUTHORIZE.value: 168,
    ApprovalKind.SELF_MODEL_DIFF.value: 336,
    ApprovalKind.EMAIL_DRAFT.value: 24,
}


DEFAULT_NOTIFY_EMAIL: dict[str, bool] = {
    ApprovalKind.SELF_MODEL_DIFF.value: True,
    ApprovalKind.USER_MODEL_DIFF.value: True,
    ApprovalKind.DREAM_DIFF.value: False,
    ApprovalKind.SENDER_AUTHORIZE.value: False,
    ApprovalKind.GROUP_AUTHORIZE.value: False,
    ApprovalKind.EMAIL_DRAFT.value: False,
    ApprovalKind.ACTION.value: False,
    ApprovalKind.IMAGE.value: False,
    ApprovalKind.MESSAGE.value: False,
}


@dataclass(frozen=True)
class ApprovalsConfig:
    enabled: bool = True
    require_telegram: bool = False
    retention_days: int = 90
    expires_in_hours: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_EXPIRES_IN_HOURS)
    )
    notify_email_by_kind: dict[str, bool] = field(
        default_factory=lambda: dict(DEFAULT_NOTIFY_EMAIL)
    )
    dkim_trusted_mta_hostnames: tuple[str, ...] = ()
    cli_disabled: bool = False
    cli_operator_uid: int | None = None


def load_approvals_config(instance_dir: Path) -> ApprovalsConfig:
    cfg_path = Path(instance_dir) / "ops" / "approvals.yaml"
    if not cfg_path.exists():
        return ApprovalsConfig()
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        try:
            from gateway.config import _parse_simple_yaml  # type: ignore

            data = _parse_simple_yaml(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return ApprovalsConfig()
    if not isinstance(data, dict):
        return ApprovalsConfig()
    return _build(data)


def _build(data: dict[str, Any]) -> ApprovalsConfig:
    expires = dict(DEFAULT_EXPIRES_IN_HOURS)
    raw_expires = data.get("expires_in_hours") or {}
    if isinstance(raw_expires, dict):
        for key, value in raw_expires.items():
            try:
                expires[str(key)] = int(value)
            except (TypeError, ValueError):
                continue

    notify = dict(DEFAULT_NOTIFY_EMAIL)
    notify_raw = data.get("notify_email_by_kind") or {}
    if isinstance(notify_raw, dict):
        for key, value in notify_raw.items():
            notify[str(key)] = bool(value)

    dkim_block = data.get("dkim") or {}
    trusted = ()
    if isinstance(dkim_block, dict):
        raw_trusted = dkim_block.get("trusted_mta_hostnames") or ()
        if isinstance(raw_trusted, (list, tuple)):
            trusted = tuple(str(v) for v in raw_trusted if v)

    cli_block = data.get("cli") or {}
    cli_disabled = bool(cli_block.get("disabled", False)) if isinstance(cli_block, dict) else False
    cli_uid: int | None = None
    if isinstance(cli_block, dict) and cli_block.get("operator_uid") is not None:
        try:
            cli_uid = int(cli_block["operator_uid"])
        except (TypeError, ValueError):
            cli_uid = None

    return ApprovalsConfig(
        enabled=bool(data.get("enabled", True)),
        require_telegram=bool(data.get("require_telegram", False)),
        retention_days=int(data.get("retention_days", 90)),
        expires_in_hours=expires,
        notify_email_by_kind=notify,
        dkim_trusted_mta_hostnames=trusted,
        cli_disabled=cli_disabled,
        cli_operator_uid=cli_uid,
    )


def kind_expires_hours(cfg: ApprovalsConfig, kind: str) -> int:
    return int(cfg.expires_in_hours.get(kind, cfg.expires_in_hours.get("default", 72)))


def kind_notify_email_default(cfg: ApprovalsConfig, kind: str) -> bool:
    return bool(cfg.notify_email_by_kind.get(kind, False))
