"""Phase classifier — maps stderr tail + mtime to a PhaseResult."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import PhaseResult


_PHASES_YAML_PATH = Path(__file__).parent / "phases.yaml"
_PHASES_CACHE: dict[str, Any] | None = None


def _load_phases(override_path: str = "") -> dict[str, Any]:
    global _PHASES_CACHE
    if override_path:
        return _load_from_path(Path(override_path))
    if _PHASES_CACHE is None:
        _PHASES_CACHE = _load_from_path(_PHASES_YAML_PATH)
    return _PHASES_CACHE


def _load_from_path(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(text) or {}
        except ImportError:
            data = {}
    except OSError:
        data = {}
    result = data.get("phases") if isinstance(data, dict) else {}
    return result if isinstance(result, dict) else {}


def classify(
    stderr_tail: str,
    *,
    mtime_age_seconds: float | None = None,
    has_stderr: bool = True,
    elapsed_seconds: float = 0.0,
    override_path: str = "",
) -> PhaseResult:
    """Classify the current phase from stderr content + mtime signal.

    Rules (in order):
    - No stderr output yet AND elapsed < 30s → `starting`
    - No stderr output yet AND elapsed >= 30s → `thinking` (brain is working, just no stderr)
    - stderr mtime_age > 30s → `idle`
    - Latest keyword match in tail → phase
    - No match → `thinking`
    """
    phases = _load_phases(override_path)

    if not has_stderr:
        phase = "starting" if elapsed_seconds < 30 else "thinking"
        return _make(phase, phases)

    if mtime_age_seconds is not None and mtime_age_seconds > 30:
        return _make("idle", phases)

    best_pos: int = -1
    best_phase: str = ""

    for phase_key, spec in phases.items():
        if not isinstance(spec, dict):
            continue
        keywords: list[str] = spec.get("keywords") or []
        if not keywords:
            continue
        for kw in keywords:
            # case-insensitive search; tool calls use mixed case e.g. "Read("
            idx = stderr_tail.rfind(kw)
            if idx == -1:
                idx = stderr_tail.lower().rfind(kw.lower())
            if idx > best_pos:
                best_pos = idx
                best_phase = phase_key

    if not best_phase:
        return _make("thinking", phases)
    return _make(best_phase, phases)


def _make(phase_key: str, phases: dict[str, Any]) -> PhaseResult:
    spec = phases.get(phase_key) or {}
    emoji = str(spec.get("emoji") or "💭")
    labels_raw = spec.get("labels") or {}
    labels: dict[str, str] = {
        str(k): str(v) for k, v in labels_raw.items()
    }
    if not labels:
        labels = {"en": phase_key}
    return PhaseResult(phase=phase_key, emoji=emoji, label=labels)
