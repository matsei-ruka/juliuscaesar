"""Supervisor AI narrator — cheap model call per event per tick.

Produces a ≤14-word sentence for the card's "Last signal" field.

Contract:
- Always returns non-empty string (falls back to phase label on any failure).
- Never leaks credentials, framework internals, or banned tokens.
- Mirrors request language (en/it).
- Callers enforce tick + event call budgets before calling this.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from gateway.channels._http import http_json
from gateway.config import env_value

from .models import EventSnapshot
from .state import EventState


LogFn = Callable[[str], None]

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_BANNED: frozenset[str] = frozenset({
    "gateway", "adapter", "brain", "juliuscaesar", "conversation_id",
    "event_id", "pid", "stderr", "queue.db",
})

_REDACT_RE = re.compile(
    r'(?i)(?:(api[_-]?key|token|secret|authorization)\s*[:=]\s*(?:bearer\s+)?\S+|\bbearer\s+\S+)',
)

_SYSTEM_PROMPT: dict[str, str] = {
    "en": (
        "You are a concise progress narrator. You describe what an AI assistant is doing "
        "in ONE sentence of at most 14 words. "
        "Use plain language. No technical jargon about the underlying framework. "
        "Never mention: gateway, adapter, brain, JuliusCaesar, conversation_id, "
        "event_id, pid, stderr, queue.db. "
        'Respond ONLY with valid JSON: {"narration": "<sentence>"}'
    ),
    "it": (
        "Sei un narratore conciso di progressi. Descrivi in UNA frase di massimo 14 parole "
        "cosa sta facendo un assistente AI. "
        "Linguaggio semplice. Nessun gergo tecnico sul framework sottostante. "
        "Non menzionare mai: gateway, adapter, brain, JuliusCaesar, conversation_id, "
        "event_id, pid, stderr, queue.db. "
        'Rispondi SOLO con JSON valido: {"narration": "<frase>"}'
    ),
}

_STDERR_USER: dict[str, str] = {
    "en": (
        "Phase: {phase}\n"
        "Recent output:\n```\n{tail}\n```\n"
        "Prior narration (avoid repeating): {prior}\n\n"
        "Narrate the latest meaningful signal in one sentence, max 14 words, in English."
    ),
    "it": (
        "Fase: {phase}\n"
        "Output recente:\n```\n{tail}\n```\n"
        "Narrazione precedente (non ripetere): {prior}\n\n"
        "Narra l'ultimo segnale significativo in una frase, max 14 parole, in italiano."
    ),
}

_NO_STDERR_USER: dict[str, str] = {
    "en": (
        "Task: {task}\n"
        "Running for: {elapsed}\n"
        "Prior narration (avoid repeating): {prior}\n\n"
        "The AI assistant is actively working. No output yet. "
        "Narrate in one sentence (max 14 words) what it is likely doing right now, in English."
    ),
    "it": (
        "Compito: {task}\n"
        "In esecuzione da: {elapsed}\n"
        "Narrazione precedente (non ripetere): {prior}\n\n"
        "L'assistente AI sta lavorando attivamente. Nessun output ancora. "
        "Narra in una frase (max 14 parole) cosa sta probabilmente facendo in questo momento, in italiano."
    ),
}


@dataclass(frozen=True)
class NarratorResult:
    text: str
    from_model: bool


def redact_stderr(tail: str) -> str:
    """Strip credential-like patterns before sending to narrator or logging."""
    def _sub(m: re.Match) -> str:
        g1 = m.group(1)
        return f"{g1}=[REDACTED]" if g1 else "[REDACTED]"
    return _REDACT_RE.sub(_sub, tail)


def _validate(text: str) -> bool:
    if not text or len(text) > 140:
        return False
    lower = text.lower()
    return not any(tok in lower for tok in _BANNED)


def _parse_brain_spec(narrator_brain: str) -> tuple[str, str]:
    if ":" in narrator_brain:
        provider, model = narrator_brain.split(":", 1)
        return provider.lower(), model
    return "openrouter", narrator_brain


def _call_model(
    instance_dir: Path,
    model: str,
    system: str,
    user: str,
    *,
    log: LogFn | None,
) -> str | None:
    api_key = env_value(instance_dir, "OPENROUTER_API_KEY")
    if not api_key:
        if log:
            log("supervisor narrator: OPENROUTER_API_KEY missing, skipping")
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 80,
    }
    try:
        data = http_json(
            _OPENROUTER_URL,
            token=api_key,
            data=payload,
            timeout=12,
        )
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"supervisor narrator HTTP error: {exc}")
        return None
    try:
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("empty choices")
        content = str((choices[0].get("message") or {}).get("content") or "")
        return content.strip()
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"supervisor narrator decode error: {exc} raw={data!r}")
        return None


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def narrate(
    snap: EventSnapshot,
    ev_state: EventState,
    narrator_brain: str,
    instance_dir: Path,
    *,
    narrator_budget: bool = True,
    log: LogFn | None = None,
) -> NarratorResult:
    """Return narration for the card's Last signal field.

    When stderr is empty (e.g. claude brain) and PID is alive, calls the AI
    using event content + elapsed time so the signal reads as natural language,
    not a hardcoded string.

    Falls back to phase label if model is unavailable or output fails validation.
    """
    lang = snap.language if snap.language in _SYSTEM_PROMPT else "en"
    fallback_text = snap.phase.label_for(lang)

    tail_raw = snap.adapter.stderr_tail or ""

    if not tail_raw and not snap.adapter.pid_alive:
        # PID dead + no stderr → brain finished; finalize will delete card.
        return NarratorResult(text=ev_state.last_narration or fallback_text, from_model=False)

    if not narrator_budget:
        return NarratorResult(text=ev_state.last_narration or fallback_text, from_model=False)

    provider, model = _parse_brain_spec(narrator_brain)
    if provider != "openrouter":
        if log:
            log(f"supervisor narrator: unsupported provider '{provider}', using fallback")
        return NarratorResult(text=fallback_text, from_model=False)

    system = _SYSTEM_PROMPT[lang]
    prior = ev_state.last_narration or "none"
    phase_label = snap.phase.label_for(lang)

    if tail_raw:
        tail_redacted = redact_stderr(tail_raw[-2000:])
        user = _STDERR_USER[lang].format(
            phase=phase_label,
            tail=tail_redacted,
            prior=prior,
        )
    else:
        # stderr empty but PID alive — narrate from task description + elapsed.
        task = (snap.event.content or "")[:300].strip() or phase_label
        elapsed = _format_elapsed(snap.age_seconds)
        user = _NO_STDERR_USER[lang].format(
            task=task,
            elapsed=elapsed,
            prior=prior,
        )

    raw = _call_model(instance_dir, model, system, user, log=log)
    if raw is None:
        return NarratorResult(text=ev_state.last_narration or fallback_text, from_model=False)

    # Parse JSON {"narration": "..."}; accept plain text as fallback
    try:
        obj = json.loads(raw)
        text = str(obj.get("narration") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        text = raw.strip()

    if not _validate(text):
        if log:
            log(f"supervisor narrator validation fail: {text!r}")
        return NarratorResult(text=fallback_text, from_model=False)

    return NarratorResult(text=text, from_model=True)
