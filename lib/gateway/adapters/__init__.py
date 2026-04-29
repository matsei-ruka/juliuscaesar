"""Direct-API adapters for the gateway.

These bypass the per-brain shell adapters in ``lib/heartbeat/adapters/`` and
call the upstream API directly. They are used for low-latency conversational
flows where the agent loop / tool surface of a CLI brain is not needed.

Currently only :mod:`codex_api` lives here — for OpenAI's Responses API via
the local Codex CLI's OAuth state.
"""
