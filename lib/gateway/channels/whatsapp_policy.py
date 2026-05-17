"""WhatsApp sender/group access control.

Matches the email channel's 3-tier pattern:

    Trusted   → Brain responds. Reply sent immediately.
    External  → Message enqueued. Brain response drafted.
                Operator notified with proposed answer for approval.
    Blocked   → Silent drop. Logged.

Policy is read from ``ops/gateway.yaml`` under ``channels.whatsapp.accounts.<id>``
for the selected account. Senders can be moved between tiers via
``jc-whatsapp chats trust|external|block <jid>``, which mutates the gateway.yaml
allow_from and blocklist lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from gateway.config_writer import atomic_write_text


@dataclass(frozen=True)
class WhatsAppPolicy:
    account_id: str = "default"
    dm_policy: str = "external"       # trusted | external | blocked
    allow_from: tuple[str, ...] = ()   # JIDs treated as Trusted
    blocklist: tuple[str, ...] = ()    # JIDs treated as Blocked
    group_policy: str = "external"
    group_allow_from: tuple[str, ...] = ()
    require_group_mention: bool = True


def _cfg_path(instance_dir: Path) -> Path:
    return Path(instance_dir) / "ops" / "gateway.yaml"


def _normalize_jid(value: str) -> str:
    return str(value or "").lower().strip()


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [_normalize_jid(v) for v in value if _normalize_jid(v)]
    text = _normalize_jid(str(value))
    return [text] if text else []


def _dedupe_sorted(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({_normalize_jid(v) for v in values if _normalize_jid(v)}))


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


def read_policy(instance_dir: Path, account_id: str = "default") -> WhatsAppPolicy:
    """Return the normalized WhatsApp access policy for an account."""
    _path, _text, data = _load_yaml(instance_dir)
    channels = data.get("channels") if isinstance(data.get("channels"), dict) else {}
    wa = channels.get("whatsapp") if isinstance(channels.get("whatsapp"), dict) else {}
    accounts = wa.get("accounts") if isinstance(wa.get("accounts"), dict) else {}
    acct = accounts.get(account_id) if isinstance(accounts.get(account_id), dict) else {}

    return WhatsAppPolicy(
        account_id=account_id,
        dm_policy=str(acct.get("dm_policy", "external")).strip() or "external",
        allow_from=_dedupe_sorted(_normalize_list(acct.get("allow_from"))),
        blocklist=_dedupe_sorted(_normalize_list(acct.get("blocklist"))),
        group_policy=str(acct.get("group_policy", "external")).strip() or "external",
        group_allow_from=_dedupe_sorted(_normalize_list(acct.get("group_allow_from"))),
        require_group_mention=bool(
            acct.get("require_group_mention", True)
        ),
    )


def resolve_tier(policy: WhatsAppPolicy, jid: str) -> str:
    """Resolve a sender JID to one of: trusted, external, blocked.

    Order: blocklist first (always wins), then allow_from, then dm_policy default.
    """
    norm = _normalize_jid(jid)
    if not norm:
        return "blocked"
    if norm in policy.blocklist:
        return "blocked"
    if norm in policy.allow_from:
        return "trusted"
    # dm_policy is the catch-all
    if policy.dm_policy == "trusted":
        return "trusted"
    if policy.dm_policy == "blocked":
        return "blocked"
    return "external"


def resolve_group_tier(policy: WhatsAppPolicy, group_jid: str) -> str:
    """Resolve a group JID to: trusted, external, blocked."""
    norm = _normalize_jid(group_jid)
    if not norm:
        return "blocked"
    if norm in policy.blocklist:
        return "blocked"
    if norm in policy.group_allow_from:
        return "trusted"
    if policy.group_policy == "trusted":
        return "trusted"
    if policy.group_policy == "blocked":
        return "blocked"
    return "external"


def group_mention_allowed(
    policy: WhatsAppPolicy,
    mentions: Iterable[str],
    self_jid: str,
) -> bool:
    """Check whether the assistant was mentioned (or quoted).

    Returns True if:
      - require_group_mention is False, OR
      - self_jid appears in mentions, OR
      - mentions is non-empty (quoted message counts as mention via protocol).
    """
    if not policy.require_group_mention:
        return True
    norm_self = _normalize_jid(self_jid)
    for mention in mentions:
        if _normalize_jid(mention) == norm_self:
            return True
    return False


def set_tier(
    instance_dir: Path,
    account_id: str,
    jid: str,
    tier: str,
) -> bool:
    """Place ``jid`` in exactly one policy tier for the account.

    ``tier`` must be one of ``trusted``, ``external``, or ``blocked``.
    Returns True iff ``ops/gateway.yaml`` changed.
    """
    tier = str(tier or "").lower().strip()
    if tier not in {"trusted", "external", "blocked"}:
        raise ValueError(f"unsupported tier: {tier!r} (use trusted, external, or blocked)")
    jid_norm = _normalize_jid(jid)
    if not jid_norm:
        raise ValueError("jid is required")

    path, original_text, data = _load_yaml(instance_dir, strict=True)

    # Navigate to the config section
    channels = data.setdefault("channels", {})
    if not isinstance(channels, dict):
        channels = {}
        data["channels"] = channels
    wa = channels.setdefault("whatsapp", {})
    if not isinstance(wa, dict):
        wa = {}
        channels["whatsapp"] = wa
    accounts = wa.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        accounts = {}
        wa["accounts"] = accounts
    acct = accounts.setdefault(account_id, {})
    if not isinstance(acct, dict):
        acct = {}
        accounts[account_id] = acct

    current = read_policy(instance_dir, account_id)
    lists = {
        "trusted": set(current.allow_from),
        "blocked": set(current.blocklist),
    }
    # external = not in either list (no explicit external list; dm_policy handles it)
    before = {name: set(values) for name, values in lists.items()}

    # Remove jid from all lists, then add to the target
    for values in lists.values():
        values.discard(jid_norm)
    if tier == "trusted":
        lists["trusted"].add(jid_norm)
    elif tier == "blocked":
        lists["blocked"].add(jid_norm)
    # external: just remove from all lists, dm_policy handles it

    if before == lists:
        return False

    acct["allow_from"] = sorted(lists["trusted"])
    if lists["blocked"]:
        acct["blocklist"] = sorted(lists["blocked"])
    else:
        acct.pop("blocklist", None)

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
