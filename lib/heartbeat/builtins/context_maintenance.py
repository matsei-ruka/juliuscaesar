"""context_maintenance — §10 idle session maintenance (inventory stub).

Scheduled idle maintenance is the proactive half of the context-aware session
lifecycle: between turns, sessions whose measured context has crossed the
``idle_maintenance_ratio`` are compacted before the next inbound turn pays the
cost. This builtin is the entry point a cron task targets with
``builtin: context_maintenance``.

Scope note (PR #85): this ships as a *read-only inventory*. It loads the
lifecycle telemetry, computes per-owner lifecycle pressure against the session
ceiling, and reports which owners are over the maintenance / rotate thresholds.
Actually rotating idle sessions from the cron path (and the full schedule
wiring) is the documented follow-up — the live `/compact` command and the
`context_exhausted` recovery handler already perform rotation on demand.

Disabled by default; operators opt in via a tasks.yaml block.
"""

from __future__ import annotations

from pathlib import Path


def run(instance_dir: Path, dry_run: bool = False) -> dict:
    """Inventory idle session-context pressure. Never rotates (stub)."""
    from gateway import queue
    from gateway.config import load_config
    from gateway.lifecycle import profiles as profiles_mod
    from gateway.lifecycle import routing as routing_mod
    from gateway.lifecycle import telemetry as telemetry_mod

    try:
        config = load_config(instance_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"config load failed: {exc}"}

    lifecycle = config.session_lifecycle
    if not lifecycle.enabled:
        return {"ok": True, "skipped": "session_lifecycle disabled", "owners": []}

    registry = lifecycle.registry()
    thresholds = lifecycle.thresholds

    conn = queue.connect(instance_dir)
    try:
        rows = telemetry_mod.list_telemetry(conn)
    finally:
        conn.close()

    owners: list[dict] = []
    over_maintenance = 0
    over_rotate = 0
    for tel in rows:
        if tel.effective_input_tokens is None:
            continue
        model = tel.last_model
        selected = registry.for_model(model) if model else None
        ceiling = (
            profiles_mod.session_ceiling(registry, model=selected.model, selected=selected)
            if selected
            else None
        )
        pressure = routing_mod.lifecycle_pressure(tel.effective_input_tokens, ceiling)
        flag = "ok"
        if pressure >= thresholds.rotate_ratio:
            flag = "rotate"
            over_rotate += 1
        elif pressure >= thresholds.idle_maintenance_ratio:
            flag = "maintain"
            over_maintenance += 1
        owners.append(
            {
                "owner_key": tel.owner_key,
                "brain": tel.brain,
                "effective_input_tokens": tel.effective_input_tokens,
                "lifecycle_pressure": round(pressure, 4) if pressure != float("inf") else None,
                "flag": flag,
            }
        )

    return {
        "ok": True,
        "dry_run": dry_run,
        "stub": "inventory-only; rotation from cron is a follow-up (PR #85)",
        "owners": owners,
        "over_maintenance": over_maintenance,
        "over_rotate": over_rotate,
    }
