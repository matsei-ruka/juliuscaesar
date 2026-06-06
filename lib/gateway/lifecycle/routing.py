"""§10–§11 — pre-dispatch routing/lifecycle pressure gate.

Two independent ratios (§10):

    routing_pressure   = required_context / selected_profile.input_capacity
    lifecycle_pressure = current_context  / session_ceiling.input_capacity

Routing pressure decides whether the triage-selected model can accept the next
turn. Lifecycle pressure decides when the session must be maintained or
rotated. The functions here are pure; the runtime wires the decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from .profiles import ContextProfile


@dataclass(frozen=True)
class Thresholds:
    observe_ratio: float = 0.50
    idle_maintenance_ratio: float = 0.60
    rotate_ratio: float = 0.70
    emergency_ratio: float = 0.85

    def validate(self) -> list[str]:
        """Ratios must be strictly increasing and below 1.0 (§9)."""
        errors: list[str] = []
        ordered = [
            ("observe_ratio", self.observe_ratio),
            ("idle_maintenance_ratio", self.idle_maintenance_ratio),
            ("rotate_ratio", self.rotate_ratio),
            ("emergency_ratio", self.emergency_ratio),
        ]
        prev = 0.0
        for name, value in ordered:
            if not (0.0 < value < 1.0):
                errors.append(f"{name}: must be between 0 and 1 (got {value})")
            if value <= prev:
                errors.append(f"{name}: must be strictly greater than the prior ratio")
            prev = value
        return errors


@dataclass(frozen=True)
class Reserves:
    output_tokens: int = 16_000
    turn_input_tokens: int = 12_000

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.output_tokens <= 0:
            errors.append("output_tokens: must be positive")
        if self.turn_input_tokens <= 0:
            errors.append("turn_input_tokens: must be positive")
        return errors


def required_context(
    *,
    last_effective_input: int,
    estimated_new_prompt: int,
    reserves: Reserves,
) -> int:
    """§10.1 required estimate with headroom for tool use and output."""
    return (
        max(0, last_effective_input)
        + max(0, estimated_new_prompt)
        + reserves.turn_input_tokens
        + reserves.output_tokens
    )


def routing_pressure(required: int, profile: ContextProfile) -> float:
    if profile.input_capacity_tokens <= 0:
        return float("inf")
    return required / profile.input_capacity_tokens


def lifecycle_pressure(current_context: int, ceiling: ContextProfile | None) -> float:
    if ceiling is None or ceiling.input_capacity_tokens <= 0:
        return float("inf")
    return max(0, current_context) / ceiling.input_capacity_tokens


# Decision actions (§10.2 decision table).
DISPATCH = "dispatch"
UPGRADE = "context_capacity_upgrade"
ROTATE = "rotate"
EMERGENCY_ROTATE = "emergency_rotate"
FAIL = "fail"


@dataclass(frozen=True)
class GuardDecision:
    action: str
    reason: str
    routing_pressure: float
    lifecycle_pressure: float
    selected_profile: ContextProfile | None = None
    upgrade_profile: ContextProfile | None = None


def evaluate_pressure(
    *,
    selected_profile: ContextProfile | None,
    ceiling: ContextProfile | None,
    required: int,
    current_context: int,
    thresholds: Thresholds,
    resumed: bool,
    larger_profiles: list[ContextProfile] | None = None,
    usage_known: bool = True,
    turn_or_age_exceeded: bool = False,
) -> GuardDecision:
    """Implements the §10.2 decision table.

    `larger_profiles` are enabled, resume-compatible profiles the guard may
    temporarily upgrade to. A profile may only replace the triage choice when
    `allow_capacity_upgrade` is true (§9).
    """
    route_p = routing_pressure(required, selected_profile) if selected_profile else float("inf")
    life_p = lifecycle_pressure(current_context, ceiling)

    if not resumed:
        return GuardDecision(
            DISPATCH, "no resumed session", route_p, life_p, selected_profile
        )

    if life_p >= thresholds.emergency_ratio:
        return GuardDecision(
            EMERGENCY_ROTATE,
            "lifecycle pressure at or above emergency ratio",
            route_p,
            life_p,
            selected_profile,
        )

    if not usage_known and turn_or_age_exceeded:
        return GuardDecision(
            ROTATE, "usage unknown but turn/age limit exceeded", route_p, life_p, selected_profile
        )

    if life_p >= thresholds.rotate_ratio:
        return GuardDecision(
            ROTATE, "lifecycle pressure at or above rotate ratio", route_p, life_p, selected_profile
        )

    if selected_profile is not None and route_p <= 1.0:
        return GuardDecision(
            DISPATCH, "selected profile safely fits", route_p, life_p, selected_profile
        )

    # Selected profile does not fit — look for an enabled compatible larger
    # profile that safely fits.
    for profile in larger_profiles or []:
        if not profile.allow_capacity_upgrade or not profile.contributes_to_ceiling:
            continue
        if routing_pressure(required, profile) <= 1.0:
            return GuardDecision(
                UPGRADE,
                "selected profile does not fit; upgrading to larger compatible profile",
                route_p,
                life_p,
                selected_profile,
                upgrade_profile=profile,
            )

    fresh_required = max(0, required - max(0, current_context))
    if resumed and selected_profile is not None and routing_pressure(fresh_required, selected_profile) <= 1.0:
        return GuardDecision(
            ROTATE,
            "selected profile safely fits after rotation",
            route_p,
            life_p,
            selected_profile,
        )

    return GuardDecision(
        FAIL,
        "no safe profile and rotation unavailable",
        route_p,
        life_p,
        selected_profile,
    )
