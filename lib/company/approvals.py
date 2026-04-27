"""Convenience entrypoint for raising + waiting on approvals.

Long-poll model: ``raise_approval`` returns ``{approval_id, callback_token}``
immediately. ``wait_for_decision`` polls ``GET /api/approvals/<id>`` with
``?wait=<seconds>`` until decided or the overall timeout expires.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from .client import CompanyClient, CompanyError
from .conf import load


SUPPORTED_TYPES = ("image", "message", "action")


def raise_approval(
    instance_dir: Path,
    *,
    title: str,
    type_: str = "action",
    payload: Optional[dict[str, Any]] = None,
    body: str = "",
    media_paths: tuple[str, ...] = (),
    expires_in_seconds: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Create an approval. Returns ``{approval_id, callback_token}`` or ``None``.

    If ``media_paths`` is non-empty, each file is uploaded via
    ``/api/approvals/<id>/media`` after the approval is created.
    """
    if type_ not in SUPPORTED_TYPES:
        raise ValueError(f"type must be one of {SUPPORTED_TYPES}, got {type_!r}")

    cfg = load(Path(instance_dir))
    if not cfg.endpoint or not cfg.api_key:
        return None

    client = CompanyClient(cfg)
    try:
        body_payload: dict[str, Any] = {
            "type": type_,
            "title": title,
            "body": body or None,
            "payload": payload or {},
        }
        if expires_in_seconds is not None:
            body_payload["expires_in_seconds"] = int(expires_in_seconds)
        result = client.post_approval(body_payload)

        approval_id = result.get("approval_id") or result.get("id")
        callback_token = result.get("callback_token")
        if approval_id and callback_token:
            for path in media_paths:
                if not path:
                    continue
                try:
                    client.upload_approval_media(
                        str(approval_id),
                        callback_token=str(callback_token),
                        path=path,
                        content_type=_guess_content_type(path),
                    )
                except CompanyError:
                    # Media upload failure shouldn't void the approval itself.
                    continue
        return result
    except CompanyError:
        return None
    finally:
        client.close()


def wait_for_decision(
    instance_dir: Path,
    *,
    approval_id: str,
    callback_token: str,
    timeout: int = 600,
    poll_chunk: int = 30,
) -> Optional[dict[str, Any]]:
    """Block until the approval is decided or ``timeout`` seconds elapse.

    Issues repeated long-poll requests with ``?wait=<poll_chunk>`` (server
    caps each at 60s). Returns the full decision dict on success, ``None``
    on timeout / transport failure.
    """
    cfg = load(Path(instance_dir))
    if not cfg.endpoint:
        return None

    client = CompanyClient(cfg)
    deadline = time.monotonic() + max(0, timeout)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            wait = max(1, min(int(remaining), int(poll_chunk)))
            try:
                result = client.get_approval(
                    approval_id, callback_token=callback_token, wait_seconds=wait
                )
            except CompanyError:
                # Transient error: brief backoff, then retry until deadline.
                time.sleep(min(5, max(1, int(remaining))))
                continue
            status = result.get("status")
            if status and status != "pending":
                return result
    finally:
        client.close()


def _guess_content_type(path: str) -> str:
    import mimetypes

    ctype, _ = mimetypes.guess_type(path)
    return ctype or "application/octet-stream"
