"""Per-kind payload validation for unified approvals."""

from __future__ import annotations

from typing import Any

from .models import ApprovalKind, ApprovalSchemaError


REQUIRED_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    ApprovalKind.SELF_MODEL_DIFF.value: (
        "proposal_id",
        "target_file",
        "target_section",
        "diff",
        "risk_class",
    ),
    ApprovalKind.DREAM_DIFF.value: (
        "diff_id",
        "artifact_path",
        "artifact_kind",
        "content_excerpt",
        "risk_class",
    ),
    ApprovalKind.USER_MODEL_DIFF.value: (
        "proposal_id",
        "target_file",
        "target_section",
        "diff",
        "risk_class",
    ),
    ApprovalKind.SENDER_AUTHORIZE.value: (
        "chat_id",
        "chat_type",
        "title",
    ),
    ApprovalKind.GROUP_AUTHORIZE.value: (
        "chat_id",
        "chat_type",
        "title",
    ),
    ApprovalKind.EMAIL_DRAFT.value: (
        "draft_id",
        "to",
        "subject",
        "body_excerpt",
    ),
    ApprovalKind.ACTION.value: ("description",),
    ApprovalKind.IMAGE.value: ("description",),
    ApprovalKind.MESSAGE.value: ("channel", "recipient", "body_preview"),
}


def validate_kind(kind: str) -> str:
    try:
        return ApprovalKind(kind).value
    except ValueError as exc:
        valid = ", ".join(k.value for k in ApprovalKind)
        raise ApprovalSchemaError(f"unknown kind {kind!r}; valid: {valid}") from exc


def validate_payload(kind: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """Confirm required keys exist for `kind`. Returns the payload normalized to dict."""
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ApprovalSchemaError("payload must be a mapping")
    required = REQUIRED_PAYLOAD_KEYS.get(kind, ())
    missing = [k for k in required if k not in payload]
    if missing:
        raise ApprovalSchemaError(
            f"payload missing required keys for kind={kind}: {', '.join(missing)}"
        )
    return payload
