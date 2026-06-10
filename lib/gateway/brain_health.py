"""Static health probes for configured brains (audit feature 5).

"Fallback brain MUST be a working brain" was enforced by nobody: a fallback
spec like ``pi:minimax-m3`` with no pi binary or auth passed config validation
and ``jc doctor``, then failed at runtime as a 300s hang exactly when the
primary was already broken (the .209 incident chain). These probes are
**static** — adapter script present + executable, CLI binary on PATH, auth
artifact hints — no live invocation (a real invoke is slow and can consume
metered quota; an opt-in live probe is deferred, see
``docs/specs/jc-config-schema-unification.md``).

Consumers: ``jc doctor`` ("Brain health" section), ``GatewayRuntime``
(startup log warnings for fallback-role failures), tests.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import brain_spec as _brain_spec
from .config import GatewayConfig, env_value


# brain → CLI binary the adapter shells out to. API-class brains (openrouter,
# codex_api) have no local binary and are probed via Brain.validate() only.
_CLI_BINARY: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "gemini": "gemini",
    "aider": "aider",
    "pi": "pi",
    "grok": "grok",
}

# brain → auth artifact whose absence is worth a warning. Device-bound or
# optional setups make these heuristics, hence warn-level, never fail.
_AUTH_HINTS: dict[str, str] = {
    "claude": "~/.claude/.credentials.json",
    "codex": "~/.codex/auth.json",
    "pi": "~/.pi/agent/auth.json",
}

# Roles whose probe failure is FAIL, not WARN: the default brain (primary
# path) and every fallback-class role — fallbacks run exactly when the
# primary is broken, so a dead fallback is a guaranteed future outage.
_CRITICAL_ROLE_PREFIXES = (
    "default_brain",
    "default_fallback_brain",
    "triage_unsafe_fallback_brain",
    "triage_backup.",
    "supervisor.recovery.fallback_brain",
)


@dataclass
class ProbeResult:
    role: str
    spec: str
    brain: str
    level: str  # "ok" | "warn" | "fail"
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        notes = "; ".join(self.problems + self.warnings)
        base = f"{self.role} = {self.spec}"
        return f"{base} — {notes}" if notes else base


def role_is_critical(role: str) -> bool:
    return any(role.startswith(p) for p in _CRITICAL_ROLE_PREFIXES)


def configured_brain_specs(
    instance_dir: Path, cfg: GatewayConfig
) -> list[tuple[str, str]]:
    """Every (role, spec) pair the runtime could route to."""
    pairs: list[tuple[str, str]] = []
    if cfg.default_brain:
        pairs.append(("default_brain", cfg.default_brain))
    for name, channel_cfg in sorted(cfg.channels.items()):
        if channel_cfg.enabled and channel_cfg.brain:
            pairs.append((f"channels.{name}.brain", channel_cfg.brain))
    if cfg.triage.fallback_brain:
        pairs.append(("default_fallback_brain", cfg.triage.fallback_brain))
    if cfg.triage.unsafe_fallback_brain:
        pairs.append(
            ("triage_unsafe_fallback_brain", cfg.triage.unsafe_fallback_brain)
        )
    for class_, spec in sorted(cfg.triage.backup.items()):
        if spec:
            pairs.append((f"triage_backup.{class_}", spec))
    # Supervisor brains live in their own config parser (lib/supervisor).
    try:
        from supervisor.config import load_config as load_supervisor_config

        sup = load_supervisor_config(instance_dir)
        if sup.enabled:
            if sup.narrator_brain:
                pairs.append(("supervisor.narrator_brain", sup.narrator_brain))
            if sup.recovery_enabled and sup.recovery_fallback_brain:
                pairs.append(
                    ("supervisor.recovery.fallback_brain", sup.recovery_fallback_brain)
                )
    except Exception:  # noqa: BLE001 — supervisor package optional in embeds
        pass
    return pairs


def probe_spec(instance_dir: Path, role: str, spec: str) -> ProbeResult:
    problems: list[str] = []
    warnings: list[str] = []
    brain = ""
    try:
        parsed = _brain_spec.parse(str(spec))
        brain = parsed.brain
    except Exception as exc:  # noqa: BLE001
        problems.append(f"unparseable brain spec: {exc}")

    if brain:
        from .brains.dispatch import brain_class

        cls = brain_class(brain)
        if cls is None:
            problems.append(f"unknown brain {brain!r} (no adapter registered)")
        else:
            try:
                instance = cls(instance_dir)
                instance.validate()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"adapter validation failed: {exc}")
            binary = _CLI_BINARY.get(brain)
            if binary and shutil.which(binary) is None:
                problems.append(f"CLI binary {binary!r} not on PATH")
            hint = _AUTH_HINTS.get(brain)
            if hint and not Path(hint).expanduser().exists():
                warnings.append(f"auth artifact missing: {hint}")
            if brain == "openrouter" and not env_value(
                instance_dir, "OPENROUTER_API_KEY"
            ):
                # validate() already fails on this; keep the message specific.
                pass

    if problems:
        level = "fail" if role_is_critical(role) else "warn"
    elif warnings:
        level = "warn"
    else:
        level = "ok"
    return ProbeResult(
        role=role,
        spec=str(spec),
        brain=brain,
        level=level,
        problems=problems,
        warnings=warnings,
    )


def probe_all(instance_dir: Path, cfg: GatewayConfig) -> list[ProbeResult]:
    seen: set[tuple[str, str]] = set()
    out: list[ProbeResult] = []
    for role, spec in configured_brain_specs(instance_dir, cfg):
        key = (role, str(spec))
        if key in seen:
            continue
        seen.add(key)
        out.append(probe_spec(instance_dir, role, spec))
    return out
