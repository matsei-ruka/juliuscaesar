"""The Company — fleet observability client for JuliusCaesar instances.

Background reporter posts gateway snapshots, worker lifecycle events, and
conversation messages to a Company backend (FastAPI + Postgres). All HTTP
calls are sync (`requests`); failures buffer to ``state/company/outbox/``
and replay on reconnect. Failures NEVER crash the gateway.

See: ``the-company`` repo / spec §6 for the integration contract.
"""

from __future__ import annotations

from . import alerts, approvals, client, conf, reporter

__all__ = ["alerts", "approvals", "client", "conf", "reporter"]
