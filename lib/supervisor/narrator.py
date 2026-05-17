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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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

_TITLE_SYSTEM: dict[str, str] = {
    "en": (
        "You are a task labeler. Given a user's message to an AI assistant, "
        "produce a short activity title of at most 6 words. "
        "Use a gerund or noun phrase (e.g. 'Auditing PHP Repository', 'Searching Auth Vulnerabilities'). "
        "No punctuation at end. No quotes. "
        'Respond ONLY with valid JSON: {"title": "<short title>"}'
    ),
    "it": (
        "Sei un classificatore di task. Data una richiesta utente, "
        "produci un titolo breve dell'attività di massimo 6 parole. "
        "Usa gerundio o sintagma nominale (es: 'Analisi Repository PHP', 'Ricerca Vulnerabilità Auth'). "
        "Nessuna punteggiatura finale. Niente virgolette. "
        'Rispondi SOLO con JSON valido: {"title": "<titolo breve>"}'
    ),
}

_TITLE_USER: dict[str, str] = {
    "en": 'User message: "{message}"\n\nGenerate a short activity title (max 6 words, gerund/noun form), in English:',
    "it": 'Messaggio: "{message}"\n\nGenera un titolo breve dell\'attività (max 6 parole, gerundio/forma nominale), in italiano:',
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


def _instance_to_project_slug(instance_dir: Path) -> str:
    """Convert instance_dir path to Claude's project directory slug."""
    return str(instance_dir).replace("/", "-").replace("_", "-")


def _find_active_journal(instance_dir: Path, started_at_iso: str | None) -> Path | None:
    """Find the actively-written claude session journal for a running event."""
    slug = _instance_to_project_slug(instance_dir)
    journals_dir = Path.home() / ".claude" / "projects" / slug
    if not journals_dir.is_dir():
        return None

    now_ts = time.time()
    cutoff = now_ts - 180  # only consider journals modified in the last 3 min

    started_epoch: float | None = None
    if started_at_iso:
        try:
            dt = datetime.fromisoformat(started_at_iso.replace("Z", "+00:00"))
            started_epoch = dt.timestamp()
        except Exception:  # noqa: BLE001
            pass

    candidates: list[tuple[float, Path]] = []
    for p in journals_dir.glob("*.jsonl"):
        try:
            stat = p.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            continue
        if started_epoch and stat.st_ctime < started_epoch - 10:
            continue  # created before event started (with 10s slack)
        candidates.append((stat.st_mtime, p))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _extract_journal_trace(journal_path: Path, max_entries: int = 30) -> str:
    """Extract recent tool_use and thinking entries from a claude session journal."""
    entries: list[str] = []
    try:
        with journal_path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                for block in (msg.get("content") or []):
                    btype = block.get("type")
                    if btype == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input") or {}
                        args = ", ".join(f"{k}={str(v)[:60]}" for k, v in inp.items())
                        entries.append(f"{name}({args})")
                    elif btype == "thinking":
                        snippet = (block.get("thinking") or "")[:120].strip()
                        if snippet:
                            entries.append(f"[thinking] {snippet}")
    except OSError:
        return ""
    return "\n".join(entries[-max_entries:])


def generate_title(
    content: str,
    narrator_brain: str,
    instance_dir: Path,
    *,
    language: str = "en",
    log: LogFn | None = None,
) -> str | None:
    """Generate a short activity title from the event's user message. Returns None on failure."""
    lang = language if language in _TITLE_SYSTEM else "en"
    provider, model = _parse_brain_spec(narrator_brain)
    if provider != "openrouter":
        return None

    short_content = content[:200].strip()
    if not short_content:
        return None

    raw = _call_model(
        instance_dir, model,
        _TITLE_SYSTEM[lang],
        _TITLE_USER[lang].format(message=short_content),
        log=log,
    )
    if raw is None:
        return None

    try:
        obj = json.loads(raw)
        title = str(obj.get("title") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        title = raw.strip()

    if not title or len(title) > 80 or any(tok in title.lower() for tok in _BANNED):
        return None
    return title


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
        # No stderr. Try live output in priority order:
        # 1. stdout sidecar (written live for all brains — pi, codex, opencode, claude)
        # 2. claude session journal (structured tool trace, claude-only)
        # 3. task+elapsed fallback
        live_trace = ""

        stdout_tail = snap.adapter.stdout_tail or ""
        if stdout_tail:
            live_trace = redact_stderr(stdout_tail[-2000:])

        if not live_trace and snap.brain.lower() == "claude":
            journal_path = _find_active_journal(instance_dir, snap.event.started_at)
            if journal_path:
                live_trace = _extract_journal_trace(journal_path)

        if live_trace:
            user = _STDERR_USER[lang].format(
                phase=phase_label,
                tail=live_trace,
                prior=prior,
            )
        else:
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

    # Parse JSON {"narration": "..."}; reject raw JSON bleed as fallback
    try:
        obj = json.loads(raw)
        text = str(obj.get("narration") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        raw_stripped = raw.strip()
        # If it looks like JSON but failed to parse, don't render it in the card.
        if raw_stripped.startswith("{") or raw_stripped.startswith("["):
            return NarratorResult(text=ev_state.last_narration or fallback_text, from_model=False)
        text = raw_stripped

    if not _validate(text):
        if log:
            log(f"supervisor narrator validation fail: {text!r}")
        return NarratorResult(text=fallback_text, from_model=False)

    return NarratorResult(text=text, from_model=True)
