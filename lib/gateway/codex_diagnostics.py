"""Codex readiness diagnostics for `jc doctor`.

Exposes pure functions so `bin/jc-doctor` can probe Codex state through a
small Python embed, and so the same logic is unit-testable without spawning
a shell.

Per docs/specs/codex-main-brain-hardening.md §Phase 6.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .brains.aliases import SHORT_NAME_ALIASES
from .config import BrainOverrideConfig, GatewayConfig


WRITE_CAPABLE_SANDBOXES = frozenset({"workspace-write", "yolo", "danger", "danger-full-access"})


@dataclass(frozen=True)
class Finding:
    level: str  # "ok" | "warn" | "info" | "fail"
    message: str


def codex_aliases() -> list[tuple[str, str]]:
    """Return short-name aliases that resolve to a Codex spec."""
    return sorted(
        (name, target)
        for name, target in SHORT_NAME_ALIASES.items()
        if target == "codex" or target.startswith("codex:")
    )


def auth_finding(home: Path) -> Finding:
    auth = home / ".codex" / "auth.json"
    if auth.is_file():
        return Finding("ok", f"codex auth file present: {auth}")
    return Finding(
        "info",
        f"codex auth file missing at {auth} (run `codex login` if you plan to use Codex)",
    )


def instance_codex_finding(instance_dir: Path, codex_home_env: str | None) -> Finding | None:
    """Report status of `<instance>/.codex/` relative to the runtime contract.

    The spec preferred path: gateway calls do NOT set CODEX_HOME (so Codex
    keeps using the operator's `~/.codex/auth.json`). When an instance ships
    a `.codex/` template but CODEX_HOME isn't pointed at it, that template
    is being silently ignored — operators should know.

    Returns None when the instance has no `.codex/` directory at all.
    """
    inst_codex = instance_dir / ".codex"
    if not inst_codex.is_dir():
        return None
    if codex_home_env and Path(codex_home_env).resolve() == inst_codex.resolve():
        return Finding("ok", f"instance .codex/ active (CODEX_HOME={inst_codex})")
    return Finding(
        "warn",
        f"instance .codex/ exists at {inst_codex} but CODEX_HOME is not pointing "
        "there — gateway Codex calls will ignore it. Either remove the directory "
        "or set CODEX_HOME explicitly.",
    )


def _resolve_codex_sandbox(override: BrainOverrideConfig | None) -> str:
    """Mirror lib/gateway/brains/codex.py:CodexBrain.extra_env precedence."""
    if override is None:
        return "read-only"
    if override.yolo:
        return "yolo"
    if override.sandbox:
        return str(override.sandbox)
    return "read-only"


def sandbox_finding(cfg: GatewayConfig) -> Finding | None:
    """Warn when default_brain is codex and the resolved sandbox is write-capable.

    Returns None when default_brain isn't codex (no warning warranted).
    """
    if cfg.default_brain != "codex":
        return None
    sandbox = _resolve_codex_sandbox(cfg.brains.get("codex"))
    if sandbox in WRITE_CAPABLE_SANDBOXES:
        return Finding(
            "warn",
            f"default_brain=codex with write-capable sandbox '{sandbox}' — main "
            "chat brain should be read-only. Set `brains.codex.sandbox: read-only` "
            "in ops/gateway.yaml.",
        )
    return Finding("ok", f"default_brain=codex sandbox: {sandbox}")


def all_findings(
    cfg: GatewayConfig,
    *,
    instance_dir: Path,
    home: Path,
    codex_home_env: str | None,
) -> list[Finding]:
    out: list[Finding] = [auth_finding(home)]
    inst = instance_codex_finding(instance_dir, codex_home_env)
    if inst is not None:
        out.append(inst)
    sb = sandbox_finding(cfg)
    if sb is not None:
        out.append(sb)
    return out


def format_alias_lines(aliases: Iterable[tuple[str, str]]) -> list[str]:
    return [f"  {name:14s} -> {target}" for name, target in aliases]
