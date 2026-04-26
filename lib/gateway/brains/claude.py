"""Claude brain wrapper.

Relies on Claude Code's own auto-loaded `CLAUDE.md` rather than the gateway
preamble, so we set `needs_l1_preamble = False`. Resume id is captured by
finding the most recent `.jsonl` session file in `~/.claude/projects/<slug>/`
modified at or after the adapter start time.

When the warm pool is enabled (via `warm_pool.enabled` config) and a manager
is supplied, `invoke()` first tries the pool path: a persistent `claude -p
--input-format=stream-json` subprocess keyed by `(conversation_id, brain,
model)`. On any pool failure (spawn error, IO error, timeout) it falls back to
the legacy single-shot subprocess path so the feature is non-breaking.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from ..queue import Event
from .base import Brain, BrainResult, newest_jsonl_stem, parse_iso, now_iso


class ClaudeBrain(Brain):
    name = "claude"
    needs_l1_preamble = False

    def capture_session_id(self, started_at: str) -> str | None:
        t0 = parse_iso(started_at)
        if t0 is None:
            return None
        slug = str(self.instance_dir).replace("/", "-").replace("_", "-")
        return newest_jsonl_stem(Path.home() / ".claude" / "projects" / slug, t0)

    def invoke(
        self,
        *,
        event: Event,
        model: str | None,
        resume_session: str | None,
        timeout_seconds: int,
        log_path: Path,
        log_event: Callable[[str], None] | None = None,
        warm_pool: object | None = None,
    ) -> BrainResult:
        if warm_pool is not None:
            try:
                return self._invoke_via_pool(
                    pool=warm_pool,
                    event=event,
                    model=model,
                    resume_session=resume_session,
                    timeout_seconds=timeout_seconds,
                    log_event=log_event,
                )
            except Exception as exc:  # noqa: BLE001
                # Pool failure -> fall back to legacy path. We log and continue;
                # operators can disable the pool entirely via config if errors
                # become noisy.
                if log_event is not None:
                    log_event(
                        f"warm_pool fallback event={event.id} reason={exc!r}"
                    )
        return super().invoke(
            event=event,
            model=model,
            resume_session=resume_session,
            timeout_seconds=timeout_seconds,
            log_path=log_path,
            log_event=log_event,
        )

    def _invoke_via_pool(
        self,
        *,
        pool: object,
        event: Event,
        model: str | None,
        resume_session: str | None,
        timeout_seconds: int,
        log_event: Callable[[str], None] | None,
    ) -> BrainResult:
        from ..warm_pool import PoolProcess
        from ..warm_pool.process import PoolProcessError

        conversation_id = event.conversation_id or f"event-{event.id}"
        key = (conversation_id, self.name, model)
        log = log_event or (lambda _msg: None)

        member = pool.get_or_create(key)  # type: ignore[attr-defined]
        prompt = self.prompt_for_event(event)
        timeout = self.override.timeout_seconds or timeout_seconds
        start = now_iso()
        wall_start = time.monotonic()
        log(
            f"warm_pool invoke event={event.id} brain={self.name} "
            f"model={model or '-'} session={member.session_id or 'none'} "
            f"messages={member.message_count}"
        )
        try:
            result = member.invoke(prompt, timeout_seconds=timeout)
        except PoolProcessError as exc:
            duration = time.monotonic() - wall_start
            log(
                f"warm_pool error event={event.id} brain={self.name} "
                f"duration={duration:.1f}s reason={exc}"
            )
            try:
                pool.evict(key)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            raise
        duration = time.monotonic() - wall_start
        log(
            f"warm_pool ok event={event.id} brain={self.name} "
            f"duration={duration:.1f}s session={result.session_id or 'none'} "
            f"stop={result.stop_reason or '-'}"
        )
        if result.is_error:
            raise RuntimeError(
                f"claude returned error: {result.error_text or 'unknown'}"
            )
        return BrainResult(
            response=(result.text or "").strip(),
            session_id=result.session_id,
        )
