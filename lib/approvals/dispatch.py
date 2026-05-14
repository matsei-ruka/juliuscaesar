"""Dispatch table: map `callback_kind` to producer-supplied apply handlers."""

from __future__ import annotations

import logging
from pathlib import Path

from . import callbacks
from .models import Approval


logger = logging.getLogger("approvals.dispatch")


def _dispatch_self_model(instance_dir: Path, record: Approval) -> dict:
    """Re-run the self_model applier with frozen-section guard re-checked."""
    from self_model import frozen_sections, store as sm_store
    from self_model.applier import apply_proposal

    proposal_id = (record.callback_payload or {}).get("proposal_id") or (
        record.payload.get("proposal_id")
    )
    if not proposal_id:
        raise RuntimeError("self_model_diff missing proposal_id")

    target_file = record.payload.get("target_file")
    target_section = record.payload.get("target_section")
    if target_file and frozen_sections.is_section_frozen(target_file, target_section):
        raise RuntimeError(
            f"frozen_section_violation: {target_section} in {target_file}"
        )

    proposal = None
    for candidate in sm_store.load_proposals(instance_dir, "staging"):
        if candidate.id == proposal_id:
            proposal = candidate
            break
    if proposal is None:
        raise RuntimeError(f"self_model proposal not in staging: {proposal_id}")
    apply_proposal(instance_dir, proposal)
    sm_store.move_proposal(instance_dir, proposal_id, "staging", "applied")
    return {"target_file": proposal.target_file, "proposal_id": proposal_id}


def _dispatch_user_model(instance_dir: Path, record: Approval) -> dict:
    from user_model.applier import apply_proposal
    from user_model import store as um_store

    proposal_id = (record.callback_payload or {}).get("proposal_id") or (
        record.payload.get("proposal_id")
    )
    if not proposal_id:
        raise RuntimeError("user_model_diff missing proposal_id")

    proposal = None
    for candidate in um_store.load_proposals(instance_dir, "staging"):
        if candidate.id == proposal_id:
            proposal = candidate
            break
    if proposal is None:
        raise RuntimeError(f"user_model proposal not in staging: {proposal_id}")
    apply_proposal(instance_dir, proposal)
    um_store.move_proposal(instance_dir, proposal_id, "staging", "applied")
    return {"target_file": proposal.target_file, "proposal_id": proposal_id}


def _dispatch_dream(instance_dir: Path, record: Approval) -> dict:
    from dream import apply as dream_apply

    diff_id = (record.callback_payload or {}).get("diff_id") or record.payload.get("diff_id")
    if not diff_id:
        raise RuntimeError("dream_diff missing diff_id")
    applied_path = dream_apply.approve(instance_dir, diff_id)
    return {"diff_id": diff_id, "applied_path": str(applied_path)}


def _dispatch_sender_authorize(instance_dir: Path, record: Approval) -> dict:
    from gateway.config import clear_config_cache
    from gateway.config_writer import (
        update_env_chat_ids,
        update_gateway_yaml_chat_lists,
    )

    chat_id = str(record.payload.get("chat_id") or "")
    if not chat_id:
        raise RuntimeError("sender_authorize missing chat_id")
    update_gateway_yaml_chat_lists(
        instance_dir,
        channel="telegram",
        allow_add=[chat_id],
        block_remove=[chat_id],
    )
    update_env_chat_ids(instance_dir, add=[chat_id])
    clear_config_cache()
    return {"chat_id": chat_id, "allowed": True}


def _dispatch_group_authorize(instance_dir: Path, record: Approval) -> dict:
    return _dispatch_sender_authorize(instance_dir, record)


def _dispatch_email_draft(instance_dir: Path, record: Approval) -> dict:
    """Hand off to jc-email drafts approve <draft_id>."""
    import os
    import shutil
    import subprocess

    draft_id = (record.callback_payload or {}).get("draft_id") or record.payload.get("draft_id")
    if not draft_id:
        raise RuntimeError("email_draft missing draft_id")
    binary = shutil.which("jc-email") or str(Path(__file__).resolve().parents[2] / "bin" / "jc-email")
    env = os.environ.copy()
    env["JC_INSTANCE_DIR"] = str(instance_dir)
    proc = subprocess.run(
        [binary, "drafts", "approve", str(draft_id)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"jc-email drafts approve failed rc={proc.returncode}: {proc.stderr[:200]}"
        )
    return {"draft_id": draft_id, "stdout": proc.stdout.strip()}


def _dispatch_passthrough(instance_dir: Path, record: Approval) -> dict:
    """Default for `action` / `image` / `message` kinds — record acknowledgement."""
    logger.info(
        "approvals: passthrough callback id=%s kind=%s — producer handles side effect",
        record.approval_id,
        record.callback_kind,
    )
    return {"acknowledged": True}


def register_defaults() -> None:
    """Install built-in apply handlers for each kind."""
    callbacks.register("self_model_diff", _dispatch_self_model)
    callbacks.register("user_model_diff", _dispatch_user_model)
    callbacks.register("dream_diff", _dispatch_dream)
    callbacks.register("sender_authorize", _dispatch_sender_authorize)
    callbacks.register("group_authorize", _dispatch_group_authorize)
    callbacks.register("email_draft", _dispatch_email_draft)
    callbacks.register("action", _dispatch_passthrough)
    callbacks.register("image", _dispatch_passthrough)
    callbacks.register("message", _dispatch_passthrough)


register_defaults()
