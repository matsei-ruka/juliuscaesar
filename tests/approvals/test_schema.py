"""Per-kind payload validation."""

from __future__ import annotations

import pytest

from approvals.models import ApprovalSchemaError
from approvals.schema import validate_kind, validate_payload


def test_validate_kind_accepts_known() -> None:
    assert validate_kind("self_model_diff") == "self_model_diff"
    assert validate_kind("sender_authorize") == "sender_authorize"


def test_validate_kind_rejects_unknown() -> None:
    with pytest.raises(ApprovalSchemaError):
        validate_kind("garbage_kind")


def test_self_model_payload_requires_keys() -> None:
    with pytest.raises(ApprovalSchemaError):
        validate_payload("self_model_diff", {"proposal_id": "x"})

    payload = {
        "proposal_id": "x",
        "target_file": "memory/L1/RULES.md",
        "target_section": "## §3",
        "diff": "diff",
        "risk_class": "SENSITIVE",
    }
    assert validate_payload("self_model_diff", payload) == payload


def test_message_kind_payload_keys() -> None:
    payload = {"channel": "telegram", "recipient": "@x", "body_preview": "hi"}
    assert validate_payload("message", payload) == payload
    with pytest.raises(ApprovalSchemaError):
        validate_payload("message", {"channel": "telegram"})


def test_action_minimal_payload() -> None:
    assert validate_payload("action", {"description": "do thing"}) == {
        "description": "do thing"
    }
    with pytest.raises(ApprovalSchemaError):
        validate_payload("action", {})
