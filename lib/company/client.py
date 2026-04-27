"""Sync HTTP client for The Company backend.

Uses ``requests``. Every call is best-effort and never raises out of the
gateway hot path: callers handle ``CompanyError`` (transport / 5xx /
non-retryable 4xx) and decide whether to buffer to the outbox.

Auth: ``Authorization: Bearer <api_key>`` after registration. The
register call itself sends the enrollment token in the body.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from .conf import HTTP_TIMEOUT_SECONDS, CompanyConfig

log = logging.getLogger("jc.company.client")


class CompanyError(Exception):
    """Raised on any non-2xx response or transport failure.

    ``status`` is the HTTP code if a response was received, else 0.
    """

    def __init__(self, message: str, *, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class CompanyClient:
    """Thin wrapper around ``requests.Session`` with auth + JSON helpers."""

    def __init__(self, cfg: CompanyConfig, *, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": f"jc-company/{cfg.framework}"})

    # --- Public API ---------------------------------------------------------

    def register(
        self,
        *,
        instance_id: str,
        name: str,
        framework: str,
        framework_version: str,
        enrollment_token: str,
    ) -> dict[str, Any]:
        """Exchange an enrollment token for a long-lived API key.

        Body: ``{instance_id, name, framework, framework_version, enrollment_token}``.
        Returns ``{agent_id, api_key}``.
        """
        body = {
            "instance_id": instance_id,
            "name": name,
            "framework": framework,
            "framework_version": framework_version,
            "enrollment_token": enrollment_token,
        }
        return self._post("/api/agents/register", body, auth=False)

    def heartbeat(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Rate-limit-exempt control-plane heartbeat.

        Snapshot keys: ``status, queue_depth, brain_runtime, triage_backend,
        channels_enabled, error_rate_5m, cpu_pct, memory_mb``.
        """
        return self._post("/api/agents/heartbeat", snapshot)

    def post_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Batched event ingest. Returns ``{accepted, rejected: [...]}``.

        Partial-success contract: 200 if any events accepted; the response
        identifies which were rejected.
        """
        return self._post("/api/events", {"events": events})

    def post_alert(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post("/api/alerts", body)

    def post_approval(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post("/api/approvals", body)

    def get_approval(self, approval_id: str, *, callback_token: str, wait_seconds: int = 0) -> dict[str, Any]:
        """Long-poll an approval. Server caps ``wait`` at 60s."""
        url = f"/api/approvals/{approval_id}"
        params = {"wait": str(wait_seconds)} if wait_seconds > 0 else None
        return self._get(url, params=params, bearer_override=callback_token)

    def upload_approval_media(
        self,
        approval_id: str,
        *,
        callback_token: str,
        path: str,
        content_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """Multipart upload of an approval payload artifact (image / audio).

        Per-file cap 50 MB, per-approval cumulative cap 200 MB (server enforced).
        """
        import os

        url = f"{self.cfg.endpoint}/api/approvals/{approval_id}/media"
        ctype = content_type or "application/octet-stream"
        try:
            with open(path, "rb") as fh:
                files = {"file": (os.path.basename(path), fh, ctype)}
                resp = self.session.post(
                    url,
                    files=files,
                    headers={"Authorization": f"Bearer {callback_token}"},
                    timeout=HTTP_TIMEOUT_SECONDS * 4,
                )
        except requests.RequestException as exc:
            raise CompanyError(f"transport: {exc}") from exc
        return self._unwrap(resp)

    def post_offline(self) -> None:
        """Best-effort offline status POST. Swallows all errors."""
        try:
            self._post("/api/agents/heartbeat", {"status": "offline"})
        except CompanyError:
            pass

    # --- Internals ----------------------------------------------------------

    def _headers(self, *, auth: bool, bearer_override: Optional[str] = None) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if auth:
            token = bearer_override or self.cfg.api_key
            if token:
                h["Authorization"] = f"Bearer {token}"
        return h

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        auth: bool = True,
        bearer_override: Optional[str] = None,
    ) -> dict[str, Any]:
        url = f"{self.cfg.endpoint}{path}"
        try:
            resp = self.session.post(
                url,
                json=body,
                headers=self._headers(auth=auth, bearer_override=bearer_override),
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise CompanyError(f"transport: {exc}") from exc
        return self._unwrap(resp)

    def _get(
        self,
        path: str,
        *,
        params: Optional[dict[str, str]] = None,
        bearer_override: Optional[str] = None,
    ) -> dict[str, Any]:
        url = f"{self.cfg.endpoint}{path}"
        try:
            resp = self.session.get(
                url,
                params=params,
                headers=self._headers(auth=True, bearer_override=bearer_override),
                # Server caps long-poll at 60s; pad a bit on our side.
                timeout=HTTP_TIMEOUT_SECONDS + 60,
            )
        except requests.RequestException as exc:
            raise CompanyError(f"transport: {exc}") from exc
        return self._unwrap(resp)

    def _unwrap(self, resp: requests.Response) -> dict[str, Any]:
        if 200 <= resp.status_code < 300:
            if not resp.content:
                return {}
            try:
                payload = resp.json()
            except ValueError:
                return {"raw": resp.text}
            return payload if isinstance(payload, dict) else {"data": payload}
        snippet = resp.text[:500] if resp.text else ""
        raise CompanyError(
            f"{resp.request.method} {resp.url} -> {resp.status_code}",
            status=resp.status_code,
            body=snippet,
        )

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass
