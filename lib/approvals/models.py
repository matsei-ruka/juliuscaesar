"""Dataclasses, enums, and exceptions for the unified approval table."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalKind(str, Enum):
    SELF_MODEL_DIFF = "self_model_diff"
    DREAM_DIFF = "dream_diff"
    USER_MODEL_DIFF = "user_model_diff"
    SENDER_AUTHORIZE = "sender_authorize"
    GROUP_AUTHORIZE = "group_authorize"
    EMAIL_DRAFT = "email_draft"
    ACTION = "action"
    IMAGE = "image"
    MESSAGE = "message"


SENSITIVE_KINDS = frozenset(
    {
        ApprovalKind.SELF_MODEL_DIFF.value,
        ApprovalKind.USER_MODEL_DIFF.value,
    }
)


class ApprovalError(RuntimeError):
    """Base class for approval-system failures."""


class ApprovalNotFound(ApprovalError):
    """Raised when a lookup misses."""


class ApprovalConflict(ApprovalError):
    """Raised when an attempt is made to flip a terminal row into a different state."""


class ApprovalSchemaError(ApprovalError):
    """Raised when a payload fails per-kind schema validation."""


@dataclass(frozen=True)
class Approval:
    """One decision record in `state/approvals.db`."""

    approval_id: str
    kind: str
    title: str
    body: str
    payload: dict[str, Any]
    status: str
    requested_at: str
    decided_at: str | None
    decided_by: str | None
    decision_channel: str | None
    expires_at: str | None
    applied_at: str | None
    callback_token: str
    callback_kind: str
    callback_payload: dict[str, Any]
    producer: str
    source_ref: str | None
    notify_telegram: bool
    notify_email: bool
    notified_at: str | None
    result: str | None
    schema_version: int = 1
    note: str | None = None
    media_paths: tuple[str, ...] = field(default_factory=tuple)

    @property
    def short_id(self) -> str:
        return self.approval_id[:8]

    def is_terminal(self) -> bool:
        return self.status != ApprovalStatus.PENDING.value

    def is_sensitive(self) -> bool:
        if self.kind in SENSITIVE_KINDS:
            return True
        risk = (self.payload or {}).get("risk_class")
        return isinstance(risk, str) and risk.upper() == "SENSITIVE"
