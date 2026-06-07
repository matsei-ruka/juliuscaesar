"""§9 — context profiles.

Capacity is a profile, not a model-family guess (§5.2). Every routed model
resolves to a `ContextProfile` carrying usable input capacity, output reserve,
and entitlement flags. The session ceiling is the largest explicitly enabled,
session-compatible profile JC is allowed to use for a brain (§6, §10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContextProfile:
    """Capacity + entitlement metadata for a canonical routed model."""

    key: str
    model: str
    variant: str = "standard"
    input_capacity_tokens: int = 200_000
    output_capacity_tokens: int = 16_000
    extended_context: bool = False
    requires_credits: bool = False
    enabled: bool = True
    allow_capacity_upgrade: bool = True
    source: str = "builtin"  # builtin | operator | discovery

    @property
    def contributes_to_ceiling(self) -> bool:
        """An extended-context profile only counts toward the ceiling when
        explicitly enabled (§9 validation)."""
        if not self.enabled:
            return False
        return True


# Built-in catalog. Concrete capacities are conservative standard-tier values;
# operators override via `session_lifecycle.model_profiles` in gateway.yaml.
# Extended/1M variants ship disabled — they must be opted into explicitly so JC
# never silently depends on paid overage (§3.2, §9).
DEFAULT_PROFILES: tuple[ContextProfile, ...] = (
    ContextProfile(
        key="claude-opus-4-8-standard",
        model="claude-opus-4-8",
        input_capacity_tokens=200_000,
    ),
    ContextProfile(
        key="claude-opus-4-8-extended",
        model="claude-opus-4-8",
        variant="extended",
        input_capacity_tokens=1_000_000,
        extended_context=True,
        requires_credits=True,
        enabled=True,
    ),
    ContextProfile(
        key="claude-sonnet-4-6-standard",
        model="claude-sonnet-4-6",
        input_capacity_tokens=200_000,
    ),
    ContextProfile(
        key="claude-haiku-4-5-standard",
        model="claude-haiku-4-5",
        input_capacity_tokens=200_000,
    ),
    ContextProfile(
        key="gpt-5-4-standard",
        model="gpt-5.4",
        input_capacity_tokens=128_000,  # 256K total = 128K input + 128K output
    ),
    ContextProfile(
        key="gpt-5-5-pro",
        model="gpt-5.5",
        input_capacity_tokens=272_000,  # Codex Pro tier: 400K total, 272K input
    ),
    ContextProfile(
        key="gemini-2-5-pro-standard",
        model="gemini-2.5-pro",
        input_capacity_tokens=1_000_000,
    ),
)


class ProfileRegistry:
    """Resolves a routed model id to its context profile(s).

    Operator overrides from `session_lifecycle.model_profiles` are merged on
    top of the built-in catalog by profile key.
    """

    def __init__(self, profiles: list[ContextProfile] | None = None) -> None:
        base = list(profiles) if profiles is not None else list(DEFAULT_PROFILES)
        self._by_key: dict[str, ContextProfile] = {p.key: p for p in base}

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> "ProfileRegistry":
        registry = cls()
        if not raw:
            return registry
        for key, spec in raw.items():
            if not isinstance(spec, dict):
                continue
            existing = registry._by_key.get(key)
            registry._by_key[key] = ContextProfile(
                key=key,
                model=str(spec.get("model") or (existing.model if existing else key)),
                variant=str(spec.get("variant") or (existing.variant if existing else "standard")),
                input_capacity_tokens=int(
                    spec.get("input_capacity_tokens")
                    if spec.get("input_capacity_tokens") is not None
                    else (existing.input_capacity_tokens if existing else 200_000)
                ),
                output_capacity_tokens=int(
                    spec.get("output_capacity_tokens")
                    if spec.get("output_capacity_tokens") is not None
                    else (existing.output_capacity_tokens if existing else 16_000)
                ),
                extended_context=bool(
                    spec.get("extended_context")
                    if spec.get("extended_context") is not None
                    else (existing.extended_context if existing else False)
                ),
                requires_credits=bool(
                    spec.get("requires_credits")
                    if spec.get("requires_credits") is not None
                    else (existing.requires_credits if existing else False)
                ),
                enabled=bool(
                    spec.get("enabled")
                    if spec.get("enabled") is not None
                    else (existing.enabled if existing else True)
                ),
                allow_capacity_upgrade=bool(
                    spec.get("allow_capacity_upgrade")
                    if spec.get("allow_capacity_upgrade") is not None
                    else (existing.allow_capacity_upgrade if existing else True)
                ),
                source="operator",
            )
        return registry

    def all(self) -> list[ContextProfile]:
        return list(self._by_key.values())

    def get(self, key: str) -> ContextProfile | None:
        return self._by_key.get(key)

    def for_model(self, model: str, *, variant: str = "standard") -> ContextProfile | None:
        """Resolve the profile for a canonical model id + variant.

        A bare model alias is not sufficient evidence that extended context is
        available, so the default variant is `standard` (§9 validation).
        """
        for profile in self._by_key.values():
            if profile.model == model and profile.variant == variant:
                return profile
        # Fall back to any standard profile for the model.
        for profile in self._by_key.values():
            if profile.model == model and profile.variant == "standard":
                return profile
        return None

    def enabled_for_model(self, model: str) -> list[ContextProfile]:
        return [
            p
            for p in self._by_key.values()
            if p.model == model and p.contributes_to_ceiling
        ]


def session_ceiling(
    registry: ProfileRegistry,
    *,
    model: str,
    selected: ContextProfile | None = None,
) -> ContextProfile | None:
    """The largest explicitly enabled, session-compatible profile for a model.

    If no larger profile is enabled, the selected/default standard profile is
    the ceiling (§10).
    """
    candidates = registry.enabled_for_model(model)
    if selected is not None and selected.contributes_to_ceiling:
        candidates.append(selected)
    if not candidates:
        return selected
    return max(candidates, key=lambda p: p.input_capacity_tokens)
