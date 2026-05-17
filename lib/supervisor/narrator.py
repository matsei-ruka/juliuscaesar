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

_PROMPTS: dict[str, dict[str, str]] = {
    "en": {
        "system": (
            "You are a concise progress narrator. You receive recent output lines "
            "from an AI assistant's tool trace and describe what it is doing "
            "in ONE sentence of at most 14 words. "
            "Use plain language. No technical jargon about the underlying framework. "
            "Never mention: gateway, adapter, brain, JuliusCaesar, conversation_id, "
            "event_id, pid, stderr, queue.db. "
            'Respond ONLY with valid JSON: {"narration": "<sentence>"}'
        ),
        "user": (
            "Phase: {phase}\n"
            "Recent output:\n```\n{tail}\n```\n"
            "Prior narration (avoid repeating): {prior}\n\n"
            "Narrate the latest meaningful signal in one sentence, max 14 words, in English."
        ),
    },
    "it": {
        "system": (
            "Sei un narratore conciso di progressi. Ricevi le ultime righe di output "
            "da un assistente AI e descrivi in UNA frase di massimo 14 parole cosa sta facendo. "
            "Linguaggio semplice. Nessun gergo tecnico sul framework sottostante. "
            "Non menzionare mai: gateway, adapter, brain, JuliusCaesar, conversation_id, "
            "event_id, pid, stderr, queue.db. "
            'Rispondi SOLO con JSON valido: {"narration": "<frase>"}'
        ),
        "user": (
            "Fase: {phase}\n"
            "Output recente:\n```\n{tail}\n```\n"
            "Narrazione precedente (non ripetere): {prior}\n\n"
            "Narra l'ultimo segnale significativo in una frase, max 14 parole, in italiano."
        ),
    },
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


def narrate(
    snap: EventSnapshot,
    ev_state: EventState,
    narrator_brain: str,
    instance_dir: Path,
    *,
    log: LogFn | None = None,
) -> NarratorResult:
    """Return narration for the card's Last signal field.

    Falls back to phase label if model is unavailable or output fails validation.
    Callers must check event + tick budgets before calling.
    """
    lang = snap.language if snap.language in _PROMPTS else "en"
    fallback_text = snap.phase.label_for(lang)

    provider, model = _parse_brain_spec(narrator_brain)
    if provider != "openrouter":
        if log:
            log(f"supervisor narrator: unsupported provider '{provider}', using fallback")
        return NarratorResult(text=fallback_text, from_model=False)

    prompts = _PROMPTS[lang]
    tail_raw = snap.adapter.stderr_tail or ""
    tail_redacted = redact_stderr(tail_raw[-2000:])
    prior = ev_state.last_narration or "none"
    phase_label = snap.phase.label_for(lang)

    system = prompts["system"]
    user = prompts["user"].format(
        phase=phase_label,
        tail=tail_redacted or "(no output yet)",
        prior=prior,
    )

    raw = _call_model(instance_dir, model, system, user, log=log)
    if raw is None:
        return NarratorResult(text=fallback_text, from_model=False)

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
