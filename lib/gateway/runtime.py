"""Gateway runtime loop: channels, dispatcher, delivery."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import overrides, queue, router, sessions
from .brains import invoke_brain
from .channel_lifecycle import ChannelLifecycle
from .channels.telegram import TelegramChannel
from .config import ChannelConfig, GatewayConfig, clear_env_cache, load_config
from .delivery import deliver_response
from .logging_setup import configure_logger
from .recovery_integration import RecoveryIntegration
from .triage import MetricsRecorder, TriageBackend, TriageCache, build_backend
from .triage.base import TriageResult


def typing_loop(
    send_typing: Callable[[str, int | None], None],
    stop_event: threading.Event,
    *,
    chat_id: str,
    message_thread_id: int | None = None,
    max_seconds: float = 60.0,
    interval: float = 4.0,
    monotonic: Callable[[], float] | None = None,
    wait: Callable[[float], bool] | None = None,
) -> None:
    """Drive a typing indicator until `stop_event` is set or `max_seconds` elapse.

    Calls `send_typing` once immediately, then again every `interval` seconds.
    Telegram's typing animation expires after ~5 s, so the default cadence
    keeps the indicator visible without spamming the API.

    `monotonic` and `wait` are injected for tests; production uses the real
    clock and the `stop_event.wait` blocking call.
    """
    if monotonic is None:
        monotonic = time.monotonic
    if wait is None:
        wait = stop_event.wait

    try:
        send_typing(chat_id, message_thread_id)
    except Exception:  # noqa: BLE001
        pass
    deadline = monotonic() + max_seconds
    while not stop_event.is_set():
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        wait_for = min(interval, remaining)
        if wait(wait_for):
            break
        try:
            send_typing(chat_id, message_thread_id)
        except Exception:  # noqa: BLE001
            pass


def decode_meta(event: queue.Event) -> dict[str, Any]:
    if not event.meta:
        return {}
    try:
        data = json.loads(event.meta)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


class GatewayRuntime:
    HEARTBEAT_INTERVAL_SECONDS = 5.0

    def __init__(
        self,
        instance_dir: Path,
        *,
        log_path: Path,
        stop_requested: Callable[[], bool],
    ):
        self.instance_dir = instance_dir
        self.config = load_config(instance_dir)
        self.log_path = log_path
        self.stop_requested = stop_requested
        self.worker_id = f"gateway-{os.getpid()}"
        self._channel_lifecycle = ChannelLifecycle(
            self.instance_dir,
            config=self.config,
            log=self.log,
            enqueue=self.enqueue,
            stop_requested=self.stop_requested,
        )
        self.threads = self._channel_lifecycle.threads
        self.channels = self._channel_lifecycle.channels
        self._triage_lock = threading.Lock()
        self._triage_backend: TriageBackend | None = None
        self.triage_cache = TriageCache(ttl_seconds=self.config.triage.cache_ttl_seconds)
        self.metrics = MetricsRecorder(self.instance_dir)
        self._json_logger = configure_logger(
            f"gateway.runtime.{os.getpid()}",
            log_path=log_path,
            max_bytes=self.config.reliability.log_max_bytes,
            backups=self.config.reliability.log_backups,
        )
        self._heartbeat_path = queue.queue_dir(self.instance_dir) / "heartbeat"
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._recovery = RecoveryIntegration(self)

    def _touch_heartbeat(self) -> None:
        """Bump the heartbeat file mtime — supervisor reads this for liveness."""
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self._heartbeat_path.touch(exist_ok=True)
        except OSError:
            pass

    def start_heartbeat(self) -> None:
        """Spawn the heartbeat ticker thread. Idempotent.

        Separate from the dispatch loop so an in-flight adapter call (which
        can take minutes) does not look wedged to the watchdog. Joined on
        `stop_heartbeat`.
        """
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._touch_heartbeat()

        def loop() -> None:
            while not self._heartbeat_stop.is_set():
                self._touch_heartbeat()
                if self._heartbeat_stop.wait(self.HEARTBEAT_INTERVAL_SECONDS):
                    return

        thread = threading.Thread(
            target=loop,
            daemon=True,
            name="gateway-heartbeat",
        )
        thread.start()
        self._heartbeat_thread = thread

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2)
            self._heartbeat_thread = None

    def _get_triage_backend(self) -> TriageBackend | None:
        with self._triage_lock:
            if self._triage_backend is None and self.config.triage.backend not in ("none", "", "always"):
                self._triage_backend = build_backend(self.config.triage, self.instance_dir)
            return self._triage_backend

    def reload_config(self) -> None:
        """Re-read ops/gateway.yaml — used by SIGHUP handlers."""
        clear_env_cache()
        self.config = load_config(self.instance_dir)
        self._channel_lifecycle.reload_config(self.config)
        with self._triage_lock:
            self._triage_backend = None
        self.triage_cache = TriageCache(ttl_seconds=self.config.triage.cache_ttl_seconds)

    def log(self, message: str, **fields: Any) -> None:
        # Drop reserved LogRecord field names to avoid clashes.
        safe = {k: v for k, v in fields.items() if v is not None and not k.startswith("_")}
        self._json_logger.info(message, extra=safe)

    def enqueue(self, **kwargs: Any) -> None:
        conn = queue.connect(self.instance_dir)
        try:
            depth = queue.counts(conn)
            queued = depth.get("queued", 0) + depth.get("running", 0)
            cap = self.config.reliability.max_queue_depth
            if cap > 0 and queued >= cap:
                self.log(
                    f"backpressure: queue depth {queued} >= {cap} — dropping {kwargs.get('source')}",
                    source=kwargs.get("source"),
                    kind="backpressure",
                )
                return
            event, inserted = queue.enqueue(conn, **kwargs)
        finally:
            conn.close()
        self.log(
            "event enqueued" if inserted else "event deduped",
            event_id=event.id,
            source=event.source,
            channel=event.source,
        )

    def start_channels(self) -> None:
        self.start_heartbeat()
        self._channel_lifecycle.start()

    def run_forever(self) -> None:
        try:
            self.start_channels()
            self.log("dispatcher started")
            while not self.stop_requested():
                self.dispatch_once()
                time.sleep(self.config.poll_interval_seconds)
            self.log("dispatcher stopping")
        finally:
            self.close()

    def close(self) -> None:
        """Stop background work and close stateful channel/log resources."""
        self.stop_heartbeat()
        self._channel_lifecycle.close()
        for handler in list(self._json_logger.handlers):
            if getattr(handler, "_jc_gateway_handler", False):
                self._json_logger.removeHandler(handler)
                handler.close()

    def dispatch_once(self) -> bool:
        conn = queue.connect(self.instance_dir)
        try:
            event = queue.claim_next(
                conn,
                worker_id=self.worker_id,
                lease_seconds=self.config.lease_seconds,
            )
        finally:
            conn.close()
        if event is None:
            return False
        from .brains import AdapterFailure

        # Pre-triage hook for outstanding auth-token round-trips. Returns True
        # iff the message was consumed by the recovery flow.
        if self._recovery.maybe_consume_auth_token(event):
            conn_t = queue.connect(self.instance_dir)
            try:
                queue.complete(conn_t, event.id, response="(auth token consumed)")
            finally:
                conn_t.close()
            self.log(f"event auth-token consumed id={event.id}")
            return True
        try:
            response = self.process_event(event)
            conn2 = queue.connect(self.instance_dir)
            try:
                queue.complete(conn2, event.id, response=response)
            finally:
                conn2.close()
            self.log(f"event done id={event.id} source={event.source}")
        except AdapterFailure as failure:
            self._recovery.handle_adapter_failure(event, failure)
        except Exception as exc:  # noqa: BLE001
            conn3 = queue.connect(self.instance_dir)
            try:
                failed = queue.fail(
                    conn3,
                    event.id,
                    error=str(exc)[:1000],
                    max_retries=self.config.max_retries,
                )
            finally:
                conn3.close()
            self.log(f"event {failed.status} id={event.id} error={exc}")
        return True

    def _maybe_triage(
        self,
        event: queue.Event,
        sticky: router.StickyHint | None,
    ) -> router.TriageHint | None:
        if sticky is not None:
            return None
        meta = decode_meta(event)
        if meta.get("brain_override"):
            return None
        if event.source == "cron" and meta.get("brain"):
            return None
        backend = self._get_triage_backend()
        if backend is None:
            return None
        cached = self.triage_cache.get(event.content)
        if cached is not None:
            self.log(
                f"triage cache hit id={event.id} class={cached.class_} "
                f"brain={cached.brain} conf={cached.confidence:.2f}",
                event_id=event.id,
                kind="triage",
            )
            return self._triage_to_hint(cached)
        try:
            result = backend.classify(event.content)
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"triage error backend={backend.name} id={event.id}: {exc}",
                event_id=event.id,
                kind="triage_error",
            )
            return None
        self.triage_cache.put(event.content, result)
        threshold = self.config.triage.confidence_threshold
        below = result.confidence < threshold
        raw_preview = (result.raw or "")[:120].replace("\n", " ")
        reasoning = (result.reasoning or "")[:120]
        self.log(
            f"triage id={event.id} backend={backend.name} class={result.class_} "
            f"brain={result.brain} conf={result.confidence:.2f} "
            f"threshold={threshold} below={below} "
            f"reason={reasoning!r} raw={raw_preview!r}",
            event_id=event.id,
            kind="triage",
        )
        try:
            self.metrics.record(result, fallback=below)
        except Exception:  # noqa: BLE001
            pass
        if result.is_unsafe():
            self.log(
                f"triage rejected event id={event.id} as unsafe",
                event_id=event.id,
                kind="triage_unsafe",
            )
            return None
        return self._triage_to_hint(result)

    def _triage_to_hint(self, result: TriageResult) -> router.TriageHint:
        # Honor per-class override map: triage may name claude:opus-4-7-1m but
        # the user might pin claude:sonnet-4-6 for "code" via triage_routing.
        spec = self.config.triage.routing.get(result.class_, result.brain)
        brain, _, model = spec.partition(":")
        return router.TriageHint(brain=brain or result.brain, model=model or None, confidence=result.confidence)

    def _resolve_sticky(self, event: queue.Event, channel: str) -> router.StickyHint | None:
        if not event.conversation_id:
            return None
        conn = queue.connect(self.instance_dir)
        try:
            sticky = sessions.get_active_sticky(
                conn,
                channel=channel,
                conversation_id=event.conversation_id,
            )
        finally:
            conn.close()
        if sticky is None:
            return None
        brain, _, model = sticky.brain.partition(":")
        return router.StickyHint(brain=brain or sticky.brain, model=model or None)

    def _resume_id(self, channel: str, conversation_id: str | None, brain: str) -> str | None:
        if not conversation_id:
            return None
        conn = queue.connect(self.instance_dir)
        try:
            existing = sessions.get_session(
                conn,
                channel=channel,
                conversation_id=conversation_id,
                brain=brain,
            )
        finally:
            conn.close()
        return existing.session_id if existing else None

    def _record_session(
        self,
        channel: str,
        conversation_id: str | None,
        brain: str,
        session_id: str,
    ) -> None:
        if not conversation_id:
            return
        conn = queue.connect(self.instance_dir)
        try:
            sessions.upsert_session(
                conn,
                channel=channel,
                conversation_id=conversation_id,
                brain=brain,
                session_id=session_id,
            )
        finally:
            conn.close()

    def process_event(self, event: queue.Event) -> str:
        meta = decode_meta(event)
        if meta.get("deliver_only"):
            response = event.content
            self._deliver_response(event.source, response, meta)
            return response

        channel = router.channel_name(event)
        event, meta = self._apply_inline_override(event, meta)

        slash = overrides.parse_slash_command(event.content)
        if slash is not None:
            return self._handle_slash(slash, event, meta, channel)

        sticky = self._resolve_sticky(event, channel)
        triage = self._maybe_triage(event, sticky)
        selection = router.route(
            event,
            cfg=self.config,
            sticky=sticky,
            triage=triage,
            confidence_threshold=self.config.triage.confidence_threshold,
            fallback_brain=self.config.triage.fallback_brain,
        )
        brain, model = selection.brain, selection.model
        if meta.get("image_path") and brain not in ("claude", "gemini"):
            vision_brain = self._select_vision_brain()
            if vision_brain and vision_brain != brain:
                self.log(
                    f"vision route id={event.id} forcing brain={vision_brain} "
                    f"(was {brain}) reason=image_path"
                )
                brain, model = vision_brain, None
        self.log(
            f"route id={event.id} channel={channel} brain={brain} "
            f"model={model or '-'} reason={selection.reason}"
        )

        resume_session = self._resume_id(channel, event.conversation_id, brain)
        self.log(
            f"session resume id={event.id} conv={event.conversation_id or '-'} "
            f"brain={brain} session={resume_session or 'none'}"
        )
        self.log(
            f"dispatch begin id={event.id} brain={brain} model={model or '-'} "
            f"resume={'yes' if resume_session else 'no'}"
        )

        typing_stop = self._start_typing(channel, meta)
        try:
            result = invoke_brain(
                instance_dir=self.instance_dir,
                event=event,
                brain=brain,
                model=model,
                resume_session=resume_session,
                timeout_seconds=self.config.adapter_timeout_seconds,
                log_path=self.log_path,
                config=self.config,
                log_event=self.log,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"dispatch failed id={event.id} brain={brain} reason={exc!r}"
            )
            raise
        finally:
            typing_stop.set()
        self.log(f"dispatch ok id={event.id} brain={brain}")

        if result.session_id:
            self._record_session(channel, event.conversation_id, brain, result.session_id)

        # Sticky brain is only set by an explicit user action: `/brain X` slash
        # or `[brain] ...` inline prefix. Triage runs every message otherwise,
        # so a "hi" followed immediately by "compare three providers" still
        # routes the second message to the appropriate brain.

        response = result.response or "(no response)"
        meta.setdefault("delivery_channel", channel)
        if meta.get("was_voice"):
            self._render_voice_reply(response, meta)
        self._deliver_response(channel, response, meta)
        return response

    def _deliver_response(
        self,
        source: str,
        response: str,
        meta: dict[str, Any],
    ) -> str | None:
        return deliver_response(
            instance_dir=self.instance_dir,
            source=source,
            response=response,
            meta=meta,
            config_channels=self.config.channels,
            live_channels=self.channels,
            log=self.log,
        )

    def _start_typing(self, channel: str, meta: dict[str, Any]) -> threading.Event:
        """Spawn a typing-indicator daemon for telegram channels.

        Returns a `threading.Event` the caller MUST set once the brain has
        finished, even on error. Returns an already-stopped event when typing
        is unavailable (non-telegram channel, no chat_id, missing token, etc.)
        so the caller never has to special-case the failure path.
        """
        stop = threading.Event()
        is_telegram = channel == "telegram" or meta.get("delivery_channel") == "telegram"
        if not is_telegram:
            return stop
        chat_id = meta.get("chat_id") or meta.get("notify_chat_id")
        if not chat_id:
            return stop
        cfg = self.config.channels.get("telegram") or ChannelConfig()
        try:
            telegram_channel = TelegramChannel(self.instance_dir, cfg, self.log)
        except Exception:  # noqa: BLE001
            return stop
        if not telegram_channel.ready():
            return stop
        thread_id = meta.get("message_thread_id")

        def loop() -> None:
            try:
                typing_loop(
                    telegram_channel.send_typing,
                    stop,
                    chat_id=str(chat_id),
                    message_thread_id=thread_id,
                )
            except Exception:  # noqa: BLE001
                pass

        thread = threading.Thread(target=loop, daemon=True, name="gateway-typing")
        thread.start()
        return stop

    def _select_vision_brain(self) -> str | None:
        """Return the first vision-capable brain whose adapter validates.

        Preference order: claude → gemini. Returns None if neither is set up
        on this host (caller falls back to the original routing decision).
        """
        from .brains.dispatch import _BRAIN_REGISTRY

        for candidate in ("claude", "gemini"):
            cls = _BRAIN_REGISTRY.get(candidate)
            if cls is None:
                continue
            try:
                cls(self.instance_dir).validate()
            except (FileNotFoundError, PermissionError):
                continue
            return candidate
        return None

    def _render_voice_reply(self, response: str, meta: dict[str, Any]) -> None:
        """Synthesize Rachel-voice OGG for `response` and stash the path in `meta`.

        Best-effort: any failure (missing voice config, TTS error, etc.) leaves
        `meta` unchanged so delivery falls back to text.
        """
        from .channels.voice import VoiceChannel

        cfg = self.config.channels.get("voice")
        if cfg is None:
            from .config import ChannelConfig

            cfg = ChannelConfig()
        try:
            voice_channel = VoiceChannel(self.instance_dir, cfg, self.log)
            ogg_path = voice_channel.send(response, meta)
        except Exception as exc:  # noqa: BLE001
            self.log(f"voice render error: {exc}")
            return
        if ogg_path:
            meta["synthesized_audio_path"] = ogg_path

    # --- override + slash plumbing ----------------------------------------

    def _apply_inline_override(
        self,
        event: queue.Event,
        meta: dict[str, Any],
    ) -> tuple[queue.Event, dict[str, Any]]:
        result = overrides.parse_inline_override(event.content)
        if result is None:
            return event, meta
        new_meta = dict(meta)
        new_meta["brain_override"] = result.spec
        # Inline `[brain] ...` is one-shot: route this message to the named
        # brain but do NOT pin sticky — next message goes through triage as
        # usual. To pin a brain across messages, use the `/brain X` slash.
        from dataclasses import replace

        event = replace(event, content=result.cleaned_content, meta=json.dumps(new_meta))
        return event, new_meta

    def _handle_slash(
        self,
        slash: overrides.SlashCommand,
        event: queue.Event,
        meta: dict[str, Any],
        channel: str,
    ) -> str:
        if slash.kind == "brain" and slash.spec and event.conversation_id:
            brain, _, model = slash.spec.partition(":")
            # Slash always pins sticky for a healthy default window even if
            # global sticky_idle_seconds is 0 — the user explicitly asked.
            self._update_sticky(
                channel,
                event.conversation_id,
                brain,
                model or None,
                idle_override=max(self.config.triage.sticky_idle_seconds, 1800),
            )
        reply = slash.reply or ""
        meta = dict(meta)
        meta.setdefault("delivery_channel", channel)
        self._deliver_response(channel, reply, meta)
        self.log(f"slash command id={event.id} kind={slash.kind} spec={slash.spec or '-'}")
        return reply

    def _update_sticky(
        self,
        channel: str,
        conversation_id: str,
        brain: str,
        model: str | None,
        *,
        idle_override: int | None = None,
    ) -> None:
        idle = idle_override if idle_override is not None else self.config.triage.sticky_idle_seconds
        if idle <= 0 or not conversation_id:
            return
        spec = f"{brain}:{model}" if model else brain
        conn = queue.connect(self.instance_dir)
        try:
            sessions.record_response(
                conn,
                channel=channel,
                conversation_id=conversation_id,
                brain=spec,
                sticky_idle_seconds=idle,
            )
        finally:
            conn.close()
