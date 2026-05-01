"""Email sender policy helpers.

The canonical policy lives in ``ops/gateway.yaml`` under
``channels.email.senders``. These helpers keep the operator CLIs from
duplicating YAML mutation rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from gateway.config_writer import atomic_write_text


@dataclass(frozen=True)
class SenderPolicy:
    trusted: tuple[str, ...] = ()
    external: tuple[str, ...] = ()
    blocklist: tuple[str, ...] = ()


def _cfg_path(instance_dir: Path) -> Path:
    return Path(instance_dir) / "ops" / "gateway.yaml"


def _normalize_addr(value: str) -> str:
    return str(value or "").lower().strip()


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [_normalize_addr(v) for v in value if _normalize_addr(v)]
    text = _normalize_addr(str(value))
    return [text] if text else []


def _dedupe_sorted(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({_normalize_addr(v) for v in values if _normalize_addr(v)}))


def _load_yaml(instance_dir: Path, *, strict: bool = False) -> tuple[Path, str, dict[str, Any]]:
    path = _cfg_path(instance_dir)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        data = yaml.safe_load(text) if text else {}
    except yaml.YAMLError as exc:
        if strict:
            raise ValueError(f"invalid YAML in {path}: {exc}") from exc
        data = {}
    if not isinstance(data, dict):
        data = {}
    return path, text, data


def read_policy(instance_dir: Path) -> SenderPolicy:
    """Return the normalized email sender policy.

    Legacy ``allowed`` entries are treated as ``trusted`` on read.
    """
    _path, _text, data = _load_yaml(instance_dir)
    channels = data.get("channels") if isinstance(data.get("channels"), dict) else {}
    email = channels.get("email") if isinstance(channels.get("email"), dict) else {}
    senders = email.get("senders") if isinstance(email.get("senders"), dict) else {}
    trusted = _normalize_list(senders.get("trusted")) + _normalize_list(senders.get("allowed"))
    external = _normalize_list(senders.get("external"))
    blocklist = _normalize_list(senders.get("blocklist"))
    return SenderPolicy(
        trusted=_dedupe_sorted(trusted),
        external=_dedupe_sorted(external),
        blocklist=_dedupe_sorted(blocklist),
    )


def set_sender_tier(instance_dir: Path, sender: str, tier: str) -> bool:
    """Place ``sender`` in exactly one policy tier.

    ``tier`` must be one of ``trusted``, ``external``, or ``blocklist``.
    Returns True iff ``ops/gateway.yaml`` changed.
    """
    tier = str(tier or "").lower().strip()
    if tier not in {"trusted", "external", "blocklist"}:
        raise ValueError(f"unsupported email sender tier: {tier}")
    sender_norm = _normalize_addr(sender)
    if not sender_norm:
        raise ValueError("email sender address is required")

    path, original_text, data = _load_yaml(instance_dir, strict=True)
    channels = data.setdefault("channels", {})
    if not isinstance(channels, dict):
        channels = {}
        data["channels"] = channels
    email = channels.setdefault("email", {})
    if not isinstance(email, dict):
        email = {}
        channels["email"] = email
    senders = email.setdefault("senders", {})
    if not isinstance(senders, dict):
        senders = {}
        email["senders"] = senders

    current = read_policy(instance_dir)
    lists = {
        "trusted": set(current.trusted),
        "external": set(current.external),
        "blocklist": set(current.blocklist),
    }
    before = {name: set(values) for name, values in lists.items()}
    for values in lists.values():
        values.discard(sender_norm)
    lists[tier].add(sender_norm)
    if before == lists and "allowed" not in senders:
        return False

    senders.pop("allowed", None)
    senders["trusted"] = sorted(lists["trusted"])
    senders["external"] = sorted(lists["external"])
    if lists["blocklist"]:
        senders["blocklist"] = sorted(lists["blocklist"])
    else:
        senders.pop("blocklist", None)

    new_text = yaml.safe_dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    if new_text == original_text:
        return False
    atomic_write_text(path, new_text)
    return True
