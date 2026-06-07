"""Gateway runtime loop: channels, dispatcher, delivery."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import string
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import actions_registry, capabilities, goal_cache, overrides, process_sessions, queue, reply_footer, router, sessions, transcripts
from .lifecycle import compaction, notify, profiles, routing, telemetry
from .channels.telegram_outbound import send_text as telegram_send_text
from .brain_output import (
    RECOVERED_ENVELOPE_ERROR,
    parse_brain_output,
    push_marker_sent,
)
from .brains import invoke_brain
from .channel_lifecycle import ChannelLifecycle
from .channels.telegram import TelegramChannel
from .config import ChannelConfig, GatewayConfig, clear_env_cache, env_value, load_config
from .delivery import deliver_response
from .logging_setup import configure_logger
from .brain_failure import BrainFailureStore
from .recovery_integration import RecoveryIntegration
from .triage import MetricsRecorder, TriageBackend, TriageCache, build_backend
from .triage.base import TriageResult

try:  # Optional: company client may be unavailable if `requests` isn't on path.
    import company as _company  # type: ignore
except Exception:  # noqa: BLE001
    _company = None  # type: ignore


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


def telegram_chat_action_for_meta(meta: dict[str, Any]) -> str:
    """Return the Telegram chat action that matches the expected reply shape."""
    return "record_voice" if meta.get("was_voice") else "typing"


def decode_meta(event: queue.Event) -> dict[str, Any]:
    if not event.meta:
        return {}
    try:
        data = json.loads(event.meta)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _seconds_since(ts: str) -> float | None:
    try:
        received = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0.0, datetime.now(timezone.utc).timestamp() - received.timestamp())


def _elapsed_seconds(event: queue.Event, monotonic_start: float | None) -> float | None:
    if monotonic_start is not None:
        return max(0.0, time.monotonic() - monotonic_start)
    return _seconds_since(event.received_at)


def _estimate_prompt_tokens(content: str | None) -> int:
    """Coarse new-prompt size estimate for the routing gate (§10.1).

    ~4 chars/token is the standard rough heuristic; the gate only needs an
    order-of-magnitude figure to detect when a turn no longer fits.
    """
    if not content:
        return 0
    return max(1, len(content) // 4)


_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "in", "on", "at", "to", "for", "of", "and", "or", "but",
    "it", "its", "this", "that", "i", "you", "we", "they", "he", "she", "what", "how",
    "do", "did", "can", "could", "will", "would", "please", "ok", "yes", "no", "not",
    "about", "with", "from", "by", "as", "be", "are", "was", "were", "have", "has",
    "had", "so", "then", "when", "where", "which", "who", "also", "just", "still",
    "now", "there", "here", "up", "down", "out", "get", "set", "all", "any", "one",
    "more", "my", "your", "our", "their", "me", "him", "her", "us", "them", "let",
    "cosa", "che", "di", "il", "la", "lo", "le", "gli", "un", "una", "come", "hai",
    "ho", "ha", "sei", "si", "sono", "era", "non", "per", "con", "su", "da",
})

_JACCARD_LOW = 0.05
_JACCARD_HIGH = 0.35

# Common imperative verbs / sentence starters that look like proper nouns when
# title-cased. Used to filter entity extraction so "Check Florian" → {"Florian"}.
_COMMON_VERBS: frozenset[str] = frozenset({
    "check", "restart", "run", "stop", "start", "verify", "fix", "reset",
    "test", "send", "show", "look", "tell", "help", "try", "use", "create",
    "open", "close", "add", "remove", "delete", "move", "copy", "read",
    "write", "update", "make", "give", "take", "status", "ping", "build",
    "deploy", "kill", "spawn", "list", "find", "post", "ship", "review",
    "controlla", "riavvia", "verifica", "ferma", "avvia", "mostra", "fai",
})

_TOKEN_RE = re.compile(r"[^\s" + re.escape(string.punctuation) + r"]+")
_ENTITY_TITLE_RE = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)*\b")
_ENTITY_ALLCAPS_RE = re.compile(r"\b[A-Z]{2,}\b")
_ENTITY_VMIP_RE = re.compile(r"\b(?:[Vv][Mm]\d+|\d{1,3}(?:\.\d{1,3}){1,3})\b")


def _tokenize(text: str) -> frozenset[str]:
    """Lowercase content-word token set: strips punctuation, stopwords, numbers."""
    if not text:
        return frozenset()
    lowered = text.lower()
    raw = _TOKEN_RE.findall(lowered)
    cleaned: set[str] = set()
    for tok in raw:
        tok = tok.strip(string.punctuation)
        if not tok or tok in _STOPWORDS:
            continue
        if tok.isdigit():
            continue
        cleaned.add(tok)
    return frozenset(cleaned)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity |A∩B|/|A∪B|. Returns 0.0 for empty sets."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


def _extract_entities(text: str) -> frozenset[str]:
    """Title-cased tokens, ALL-CAPS (>=2 chars), and VM/IP patterns.

    Common imperative verbs and stopwords are filtered out of the title-case
    pass so sentence-initial words like "Check" or "Restart" don't pollute
    entity sets.
    """
    if not text:
        return frozenset()
    out: set[str] = set()
    for tok in _ENTITY_TITLE_RE.findall(text):
        low = tok.lower()
        if low in _STOPWORDS or low in _COMMON_VERBS:
            continue
        out.add(tok)
    out.update(_ENTITY_ALLCAPS_RE.findall(text))
    out.update(_ENTITY_VMIP_RE.findall(text))
    return frozenset(out)


def _parse_slot_verdict(text: str) -> int | None:
    """Decode the classifier reply (`related:<N>` or `unrelated`).

    Tolerant of surrounding whitespace, code-fence backticks, and quoted
    output. Returns None for `unrelated`, an unparseable verdict, or a
    non-integer slot id.
    """
    raw = (text or "").strip().strip("`'\"").lower()
    if not raw or raw.startswith("unrelated"):
        return None
    if raw.startswith("related:"):
        tail = raw.split(":", 1)[1].strip().strip("`'\"")
        try:
            return int(tail)
        except (TypeError, ValueError):
            return None
    return None


def _lib_dir_newest_mtime(lib_dir: Path) -> tuple[float, str]:
    """Return (mtime, basename) of the newest .py file under lib_dir.

    Used to detect framework code drift: when files on disk are newer than
    the running process start, in-memory modules are stale and a respawn is
    required. Returns (0.0, "") if scan fails or no .py files are present.
    """
    newest_mtime = 0.0
    newest_name = ""
    try:
        for py in lib_dir.rglob("*.py"):
            try:
                mt = py.stat().st_mtime
            except (OSError, FileNotFoundError):
                continue
            if mt > newest_mtime:
                newest_mtime = mt
                newest_name = py.name
    except (OSError, FileNotFoundError):
        return 0.0, ""
    return newest_mtime, newest_name


class _LeaseHeartbeat:
    """Background thread that renews a SQLite queue lease while a brain call
    runs. Stops on ``__exit__`` or when the lease is lost (row no longer owned
    by ``worker_id``).

    Interval is ``max(30, lease_seconds // 3)`` — renewing roughly three times
    per lease window keeps the row owned without spamming the DB.
    """

    def __init__(
        self,
        *,
        instance_dir: Path,
        event_ids: list[int],
        worker_id: str,
        lease_seconds: int,
        log: Callable[..., None],
    ) -> None:
        self._instance_dir = instance_dir
        self._event_ids = list(event_ids)
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._log = log
        self._interval = max(30.0, float(lease_seconds) / 3.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_LeaseHeartbeat":
        if not self._event_ids or self._lease_seconds <= 0:
            return self
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"gateway-lease-hb-{','.join(str(i) for i in self._event_ids[:3])}",
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                conn = queue.connect(self._instance_dir)
                try:
                    renewed = queue.renew_lease(
                        conn,
                        self._event_ids,
                        worker_id=self._worker_id,
                        lease_seconds=self._lease_seconds,
                    )
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"lease heartbeat error ids={self._event_ids} error={exc}",
                    kind="lease_hb_error",
                )
                continue
            if renewed == 0:
                self._log(
                    f"lease heartbeat lost ids={self._event_ids} "
                    f"worker={self._worker_id} — stopping heartbeat",
                    kind="lease_hb_lost",
                )
                return


class GatewayRuntime:
    HEARTBEAT_INTERVAL_SECONDS = 5.0
    TRIAGE_REJECTION_MESSAGE = (
        "I can't help with that request. It looks outside the assistant's "
        "safety policy, so I did not pass it to a brain."
    )

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
        self.session_id = str(uuid.uuid4())
        # Code-drift detection: capture startup time + framework `lib/` root.
        # `run_forever` periodically rescans .py mtimes; if any have been
        # updated past startup, the process exits so the watchdog respawns it
        # with fresh modules. Without this, a `git pull` that touches e.g.
        # `sessions.py` silently breaks every dispatch with
        # `TypeError: unexpected keyword argument` until manual restart.
        self._startup_time = time.time()
        self._lib_dir = Path(__file__).resolve().parent.parent
        self._code_drift_last_check = 0.0
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
        self._brain_failure = BrainFailureStore(self.instance_dir)
        self._company_reporter = self._init_company_reporter()
        # Parallel-slot bookkeeping. `_busy_slots` maps (channel, conv_id) → set
        # of slot ids currently running a brain invocation. Only used when
        # `config.parallel.max_concurrent > 1`; the N=1 dispatch path never
        # touches it so behavior stays byte-identical with the serial gateway.
        self._slot_busy_lock = threading.Lock()
        self._busy_slots: dict[tuple[str, str], set[int]] = {}
        self._slot_active_threads: set[threading.Thread] = set()
        # Memoize classifier verdicts per (event content, slot summary tuple)
        # so the same message doesn't pay the openrouter call twice. TTL is
        # config-driven (`parallel.classifier.cache_ttl_seconds`).
        self._slot_classifier_cache: dict[tuple[str, str], tuple[float, str]] = {}

    def _init_company_reporter(self) -> Any:
        """Build a company.Reporter iff company integration is configured.

        Off-by-default: requires ``COMPANY_ENDPOINT`` + (api_key or
        enrollment_token) and ``company.enabled: true`` in gateway.yaml.
        """
        if _company is None:
            return None
        try:
            if not _company.conf.is_enabled(self.instance_dir):  # type: ignore[attr-defined]
                return None
            return _company.reporter.Reporter(  # type: ignore[attr-defined]
                self.instance_dir, log_event=self.log
            )
        except Exception:  # noqa: BLE001
            return None

    def _touch_heartbeat(self) -> None:
        """Bump the heartbeat file mtime — supervisor reads this for liveness.

        Also update the process session heartbeat for orphan detection.
        """
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self._heartbeat_path.touch(exist_ok=True)
        except OSError:
            pass
        try:
            process_sessions.update_heartbeat(self.instance_dir, self.session_id)
        except Exception:  # noqa: BLE001
            pass  # Non-critical; don't let heartbeat track failure block polling

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
                self._triage_backend = build_backend(
                    self.config.triage,
                    self.instance_dir,
                    codex_auth_cfg=self.config.codex_auth,
                )
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
        if inserted:
            self._log_inbound_transcript(event)

    _TRANSCRIPT_CHANNELS = ("telegram", "slack", "discord", "voice")

    def _log_inbound_transcript(self, event: queue.Event) -> None:
        """Append the inbound user message to its conversation transcript.

        Only chat channels participate — cron / jc-events fan-outs are not
        conversations. Best-effort: any failure is swallowed so a transcript
        write cannot block the dispatch loop.
        """
        if event.source not in self._TRANSCRIPT_CHANNELS:
            return
        meta = decode_meta(event)
        try:
            transcripts.append(
                self.instance_dir,
                conversation_id=event.conversation_id,
                role="user",
                text=event.content,
                message_id=event.source_message_id,
                channel=event.source,
                chat_id=str(meta.get("chat_id") or event.conversation_id or ""),
                ts=event.received_at,
            )
        except Exception:  # noqa: BLE001
            pass

    def _log_outbound_transcript(
        self,
        event: queue.Event,
        response: str,
        meta: dict[str, Any],
        channel: str,
    ) -> None:
        if channel not in self._TRANSCRIPT_CHANNELS:
            return
        if not response:
            return
        try:
            transcripts.append(
                self.instance_dir,
                conversation_id=event.conversation_id,
                role="assistant",
                text=response,
                message_id=None,
                channel=channel,
                chat_id=str(meta.get("chat_id") or event.conversation_id or ""),
            )
        except Exception:  # noqa: BLE001
            pass

    def start_channels(self) -> None:
        # Register this gateway process session for tracking + orphan detection.
        process_sessions.register_session(
            self.instance_dir,
            session_id=self.session_id,
            gateway_pid=os.getpid(),
            brain_pid=None,  # TODO: track brain PID when we subprocess Claude
            brain_type="claude",
            adapter="telegram",
        )
        self.start_heartbeat()
        self._channel_lifecycle.start()
        if self._company_reporter is not None:
            try:
                self._company_reporter.start()
            except Exception as exc:  # noqa: BLE001
                self.log(f"company reporter start failed: {exc}", kind="company_error")

    def run_forever(self) -> None:
        try:
            self.start_channels()
            self.log("dispatcher started")
            while not self.stop_requested():
                self._check_code_drift()
                self.dispatch_once()
                time.sleep(self.config.poll_interval_seconds)
            self.log("dispatcher stopping")
        finally:
            self.close()

    def _check_code_drift(self) -> None:
        """Exit the process when framework code on disk is newer than startup.

        Throttled to once per minute. On drift, logs the offending file and
        raises SystemExit(42); the watchdog respawns the gateway with fresh
        in-memory modules. The 60-second tolerance avoids racing file writes
        during startup. Future-dated files (git checkouts sometimes preserve
        committer timestamps across timezones) are ignored to prevent restart
        loops on filesystems with clock skew.
        """
        now = time.time()
        if now - self._code_drift_last_check < 60.0:
            return
        self._code_drift_last_check = now
        newest_mtime, newest_name = _lib_dir_newest_mtime(self._lib_dir)
        if newest_mtime <= self._startup_time + 60.0:
            return
        if newest_mtime > now + 60.0:
            return  # future-dated file; treat as filesystem skew, not drift
        drift_seconds = int(newest_mtime - self._startup_time)
        self.log(
            f"code drift detected — {newest_name} updated {drift_seconds}s after "
            f"process start; exiting for watchdog respawn",
            kind="code_drift_exit",
        )
        raise SystemExit(42)

    def close(self) -> None:
        """Stop background work and close stateful channel/log resources."""
        self.stop_heartbeat()
        if self._company_reporter is not None:
            try:
                self._company_reporter.stop()
            except Exception as exc:  # noqa: BLE001
                self.log(f"company reporter stop failed: {exc}", kind="company_error")
        self._channel_lifecycle.close()
        try:
            process_sessions.unregister_session(self.instance_dir, self.session_id)
        except Exception:  # noqa: BLE001
            pass  # Non-critical; don't let cleanup failure block shutdown
        for handler in list(self._json_logger.handlers):
            if getattr(handler, "_jc_gateway_handler", False):
                self._json_logger.removeHandler(handler)
                handler.close()

    def dispatch_once(self) -> bool:
        max_concurrent = self.config.parallel.max_concurrent
        conn = queue.connect(self.instance_dir)
        try:
            if max_concurrent > 1:
                # Parallel slot dispatch claims one event at a time; coalescing
                # is suppressed because messages on different slots run in
                # parallel and shouldn't get bundled.
                single = queue.claim_next(
                    conn,
                    worker_id=self.worker_id,
                    lease_seconds=self.config.lease_seconds,
                )
                events_batch = [single] if single is not None else []
            elif self.config.reliability.coalesce_same_conversation:
                events_batch = queue.claim_batch_same_conversation(
                    conn,
                    worker_id=self.worker_id,
                    lease_seconds=self.config.lease_seconds,
                )
            else:
                single = queue.claim_next(
                    conn,
                    worker_id=self.worker_id,
                    lease_seconds=self.config.lease_seconds,
                )
                events_batch = [single] if single is not None else []
        finally:
            conn.close()
        if not events_batch:
            return False
        from .brains import AdapterFailure

        if len(events_batch) == 1:
            event = events_batch[0]
            batch_ids = [event.id]
        else:
            event = self._bundle_events(events_batch)
            batch_ids = [e.id for e in events_batch]
            self.log(
                f"coalesce: claimed {len(events_batch)} events "
                f"conv_id={event.conversation_id} ids={batch_ids}"
            )

        # Pre-triage hook for outstanding auth-token round-trips. Returns True
        # iff the message was consumed by the recovery flow.
        if self._recovery.maybe_consume_auth_token(event):
            conn_t = queue.connect(self.instance_dir)
            try:
                for eid in batch_ids:
                    try:
                        queue.complete(
                            conn_t,
                            eid,
                            response="(auth token consumed)",
                            expected_locked_by=self.worker_id,
                        )
                    except KeyError:
                        # Lease lost (e.g. supervisor reset / re-claim). Skip.
                        self.log(
                            f"complete skipped id={eid} reason=lease_lost "
                            f"worker={self.worker_id}"
                        )
            finally:
                conn_t.close()
            if len(batch_ids) > 1:
                self.log(f"coalesce: marked {len(batch_ids)} events done ids={batch_ids}")
            self.log(f"event auth-token consumed id={event.id}")
            return True

        # Task-goal lifecycle (PR #65). Done here on the dispatch-loop thread so
        # the goal cache stays single-writer regardless of parallel slots.
        #   task_assigned → set the anchor, then dispatch normally (brain works it)
        #   task_closed   → clear the anchor; control event, never hits the brain
        if self._apply_goal_lifecycle(event, batch_ids):
            return True

        # Parallel mode: hand the (already-claimed) event off to a slot worker.
        # The worker thread owns the row's complete/fail transition; we return
        # immediately so the dispatch loop can claim the next event.
        if max_concurrent > 1 and len(events_batch) == 1:
            self._dispatch_parallel(event)
            return True
        try:
            with self._lease_heartbeat(batch_ids):
                response = self.process_event(event)
            conn2 = queue.connect(self.instance_dir)
            try:
                for eid in batch_ids:
                    try:
                        queue.complete(
                            conn2,
                            eid,
                            response=response,
                            expected_locked_by=self.worker_id,
                        )
                    except KeyError:
                        self.log(
                            f"complete skipped id={eid} reason=lease_lost "
                            f"worker={self.worker_id}"
                        )
            finally:
                conn2.close()
            self._cancel_reengage_on_inbound_reply(event)
            if len(batch_ids) > 1:
                self.log(f"coalesce: marked {len(batch_ids)} events done ids={batch_ids}")
            self.log(f"event done id={event.id} source={event.source}")
        except AdapterFailure as failure:
            self._recovery.handle_adapter_failure(event, failure)
        except Exception as exc:  # noqa: BLE001
            conn3 = queue.connect(self.instance_dir)
            try:
                failed_status: str | None = None
                for eid in batch_ids:
                    try:
                        failed = queue.fail(
                            conn3,
                            eid,
                            error=str(exc)[:1000],
                            max_retries=self.config.max_retries,
                            expected_locked_by=self.worker_id,
                        )
                        failed_status = failed.status
                    except KeyError:
                        self.log(
                            f"fail skipped id={eid} reason=lease_lost "
                            f"worker={self.worker_id}"
                        )
            finally:
                conn3.close()
            if len(batch_ids) > 1:
                self.log(
                    f"coalesce: marked {len(batch_ids)} events {failed_status} ids={batch_ids}"
                )
            self.log(f"event {failed_status} id={event.id} error={exc}")
        return True

    @staticmethod
    def _bundle_events(events: list[queue.Event]) -> queue.Event:
        """Merge a same-conversation batch into a single synthetic Event.

        - `content` joins per-event lines prefixed with `@username:` (falls
          back to user_id, then "user"). The `@` prefix is load-bearing: a
          `[name]` prefix would collide with `parse_inline_override`.
        - `meta` / source_message_id / user_id come from the LATEST event so
          reply context (reply_to_message_id, image_path, etc) tracks the most
          recent message. `meta.coalesced_ids` records the batch member ids;
          `process_event` uses that flag to skip slash + inline-override
          parsing (the bundled prefix-and-content shape isn't user-authored).
        - `id` is the FIRST event's id for stable logging.
        """
        latest = events[-1]
        first = events[0]
        lines: list[str] = []
        for ev in events:
            try:
                ev_meta = json.loads(ev.meta) if ev.meta else {}
            except (TypeError, ValueError):
                ev_meta = {}
            username = None
            if isinstance(ev_meta, dict):
                username = ev_meta.get("username") or ev_meta.get("user_name")
            who = username or ev.user_id or "user"
            lines.append(f"@{who}: {ev.content}")
        bundled_content = "\n\n".join(lines)
        try:
            latest_meta = json.loads(latest.meta) if latest.meta else {}
        except (TypeError, ValueError):
            latest_meta = {}
        if not isinstance(latest_meta, dict):
            latest_meta = {}
        latest_meta["coalesced_ids"] = [e.id for e in events]
        bundled_meta = json.dumps(latest_meta, sort_keys=True, separators=(",", ":"))
        return dataclasses.replace(
            latest, id=first.id, content=bundled_content, meta=bundled_meta
        )

    def _persist_slot_in_meta(self, event: queue.Event, slot: int) -> None:
        """Write the assigned slot into event.meta so the supervisor can show it.

        Supervisor runs in a separate process and doesn't see the in-memory
        slot assignment. Persisting it on the event row makes it readable via
        `queue.row_to_event` without a new schema column.
        """
        meta = decode_meta(event)
        if meta.get("slot") == int(slot):
            return
        meta["slot"] = int(slot)
        conn = queue.connect(self.instance_dir)
        try:
            queue.update_meta(conn, event.id, meta)
        except KeyError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.log(f"persist_slot_in_meta failed event={event.id} slot={slot}: {exc}")
        finally:
            conn.close()

    def _cancel_reengage_on_inbound_reply(self, event: queue.Event) -> None:
        """Cancel pending re-engagement touches when a tracked chat replies."""
        if event.source not in self._TRANSCRIPT_CHANNELS:
            return
        meta = decode_meta(event)
        chat_id = meta.get("chat_id") or event.conversation_id
        if not chat_id:
            return
        try:
            from reengage.queuer import cancel_if_tracked  # noqa: WPS433

            canceled = cancel_if_tracked(self.instance_dir, str(chat_id))
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"reengage reset failed for chat={chat_id}: {exc}",
                kind="reengage_reset",
                chat_id=str(chat_id),
            )
            return
        if canceled:
            self.log(
                f"reengage reset canceled {len(canceled)} pending touch(es) for chat={chat_id}",
                kind="reengage_reset",
                chat_id=str(chat_id),
            )

    def _maybe_triage(
        self,
        event: queue.Event,
        sticky: router.StickyHint | None,
    ) -> tuple[router.TriageHint | None, bool]:
        if sticky is not None:
            return None, False
        meta = decode_meta(event)
        if meta.get("brain_override"):
            return None, False
        if event.source == "cron" and meta.get("brain"):
            return None, False
        if meta.get("was_voice"):
            result = TriageResult(class_="voice", confidence=1.0, raw="voice attachment")
            hint = self._triage_to_hint(result)
            self.log(
                f"triage voice override id={event.id} routed={hint.full_spec() if hint else '-'}",
                event_id=event.id,
                kind="triage",
            )
            try:
                self.metrics.record(result, brain=hint.full_spec() if hint else "", fallback=False)
            except Exception:  # noqa: BLE001
                pass
            return hint, False
        backend = self._get_triage_backend()
        if backend is None:
            return None, False
        cached = self.triage_cache.get(event.content)
        if cached is not None:
            hint = self._triage_unsafe_fallback_hint(cached) if cached.is_unsafe() else self._triage_to_hint(cached)
            self.log(
                f"triage cache hit id={event.id} class={cached.class_} "
                f"routed={hint.full_spec() if hint else '-'} "
                f"conf={cached.confidence:.2f}",
                event_id=event.id,
                kind="triage",
            )
            if cached.is_unsafe():
                self._log_unsafe_verdict(event, hint)
                return hint, hint is None
            if self.config.pin_to_default_brain:
                return None, False
            return hint, False
        try:
            result = backend.classify(event.content)
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"triage error backend={backend.name} id={event.id}: {exc}",
                event_id=event.id,
                kind="triage_error",
            )
            return None, False
        self.triage_cache.put(event.content, result)
        threshold = self.config.triage.confidence_threshold
        below = result.confidence < threshold
        raw_preview = (result.raw or "")[:120].replace("\n", " ")
        hint = self._triage_unsafe_fallback_hint(result) if result.is_unsafe() else self._triage_to_hint(result)
        unsafe_fallback = result.is_unsafe() and hint is not None
        if unsafe_fallback:
            metric_brain = hint.full_spec()
        elif result.is_unsafe():
            metric_brain = ""
        elif below and self.config.triage.fallback_brain:
            metric_brain = self.config.triage.fallback_brain
        elif hint is not None:
            metric_brain = hint.full_spec()
        else:
            metric_brain = ""
        self.log(
            f"triage id={event.id} backend={backend.name} class={result.class_} "
            f"routed={hint.full_spec() if hint else '-'} conf={result.confidence:.2f} "
            f"threshold={threshold} below={below} "
            f"raw={raw_preview!r}",
            event_id=event.id,
            kind="triage",
        )
        try:
            self.metrics.record(result, brain=metric_brain, fallback=below or unsafe_fallback)
        except Exception:  # noqa: BLE001
            pass
        if result.is_unsafe():
            self._log_unsafe_verdict(event, hint)
            return hint, hint is None
        if self.config.pin_to_default_brain:
            return None, False
        return hint, False

    def _triage_unsafe_fallback_hint(self, result: TriageResult) -> router.TriageHint | None:
        spec = self.config.triage.unsafe_fallback_brain.strip()
        if not spec:
            return None
        brain, _, model = spec.partition(":")
        if not brain:
            return None
        return router.TriageHint(brain=brain, model=model or None, confidence=result.confidence)

    def _log_unsafe_verdict(
        self,
        event: queue.Event,
        hint: router.TriageHint | None,
    ) -> None:
        self.log(
            f"triage rejected event id={event.id} as unsafe",
            event_id=event.id,
            kind="triage_unsafe",
        )
        if hint is not None:
            self.log(
                f"triage unsafe-fallback id={event.id} routed={hint.full_spec()}",
                event_id=event.id,
                kind="triage_unsafe_fallback",
            )

    def _notify_triage_rejection(
        self,
        event: queue.Event,
        meta: dict[str, Any],
        channel: str,
    ) -> str:
        notice = self.TRIAGE_REJECTION_MESSAGE
        meta = dict(meta)
        meta.setdefault("delivery_channel", channel)
        self._deliver_response(channel, notice, meta)
        self._log_outbound_transcript(event, notice, meta, channel)
        self.log(
            f"triage rejection notified sender id={event.id}",
            event_id=event.id,
            kind="triage_rejection_notice",
        )
        return notice

    def _triage_to_hint(self, result: TriageResult) -> router.TriageHint | None:
        spec = self.config.triage.routing.get(result.class_) or self.config.triage.fallback_brain
        if not spec:
            return None
        brain_name = spec.partition(":")[0]
        if self._brain_failure.is_failed(brain_name):
            backup_spec = self.config.triage.backup.get(result.class_)
            if backup_spec:
                self.log(
                    f"brain_backup: class={result.class_} primary={spec} failed"
                    f" → backup={backup_spec}"
                )
                spec = backup_spec
        brain, _, model = spec.partition(":")
        if not brain:
            return None
        return router.TriageHint(brain=brain, model=model or None, confidence=result.confidence)

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

    def _resume_id(
        self,
        channel: str,
        conversation_id: str | None,
        brain: str,
        *,
        slot: int = 0,
    ) -> str | None:
        if not conversation_id:
            return None
        conn = queue.connect(self.instance_dir)
        try:
            existing = sessions.get_session(
                conn,
                channel=channel,
                conversation_id=conversation_id,
                brain=brain,
                slot=slot,
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
        *,
        slot: int = 0,
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
                slot=slot,
            )
        finally:
            conn.close()

    # --- parallel slots --------------------------------------------------

    def _pick_slot(self, event: queue.Event) -> tuple[int, bool]:
        """Return `(slot, should_queue)` for `event` under parallel dispatch.

        - For `max_concurrent <= 1` always returns `(0, False)` — serial path.
        - Otherwise asks the relatedness classifier (best-effort) and follows
          the spec rules (docs/specs/parallel-slots.md §Dispatch logic):
            related→busy  → enqueue behind that slot
            related→free  → resume that slot
            unrelated+free → pick LRU free slot (tie-break: lowest id)
            all busy      → queue on slot 0 (main lane)
        """
        max_concurrent = self.config.parallel.max_concurrent
        if max_concurrent <= 1:
            return 0, False
        channel = router.channel_name(event)
        conv = event.conversation_id or ""
        if not conv:
            # No conversation_id → no slot affinity to compute. Run on slot 0.
            return 0, False
        with self._slot_busy_lock:
            busy = set(self._busy_slots.get((channel, conv), set()))
        free = [i for i in range(max_concurrent) if i not in busy]

        # Classifier hint (None = unrelated / no opinion).
        related: int | None = None
        try:
            summaries = self._slot_summaries(channel, conv, max_concurrent)
            if summaries:
                related = self._classify_slot_affinity(event, summaries)
        except Exception as exc:  # noqa: BLE001
            self.log(f"slot classifier error id={event.id}: {exc}", kind="slot_classifier_error")
            related = None

        if related is not None and 0 <= related < max_concurrent:
            if related in busy:
                # When the busy related slot holds a backgrounded session, the new
                # inbound is a fresh primary — route it to a free slot instead of
                # queuing behind the background task indefinitely.
                if free and self.config.actions.enabled and actions_registry.has_backgrounded_for_conversation(conv):
                    picked = self._lru_free_slot(channel, conv, free)
                    self.log(
                        f"slot pick id={event.id} related={related} busy=yes backgrounded → free={picked}",
                        kind="slot_pick",
                    )
                    return picked, False
                self.log(
                    f"slot pick id={event.id} related={related} busy=yes → queue",
                    kind="slot_pick",
                )
                return related, True
            self.log(
                f"slot pick id={event.id} related={related} busy=no → resume",
                kind="slot_pick",
            )
            return related, False

        if free:
            picked = self._lru_free_slot(channel, conv, free)
            self.log(
                f"slot pick id={event.id} unrelated → lru-free={picked}",
                kind="slot_pick",
            )
            return picked, False

        self.log(
            f"slot pick id={event.id} all-busy → queue slot 0",
            kind="slot_pick",
        )
        return 0, True

    def _lru_free_slot(self, channel: str, conv: str, free: list[int]) -> int:
        """Pick the free slot with the oldest `sessions.updated_at`.

        Tie-break: lowest slot id. Falls back to lowest free id when no slot
        row exists yet (cold conversation). Mirrors spec rule
        "unrelated + free → LRU".
        """
        conn = queue.connect(self.instance_dir)
        try:
            brain = self.config.default_brain
            rows = sessions.list_sessions_for_conversation(
                conn,
                channel=channel,
                conversation_id=conv,
                brain=brain,
            )
        finally:
            conn.close()
        updated_by_slot = {r.slot: r.updated_at for r in rows}
        # Slot with no row counts as oldest (never used); pick that first.
        candidates = sorted(
            free,
            key=lambda s: (updated_by_slot.get(s, ""), s),
        )
        return candidates[0]

    def _slot_summaries(
        self,
        channel: str,
        conv: str,
        max_concurrent: int,
    ) -> dict[int, str]:
        """Recent activity summary per slot (last ~3 user turns), for classifier.

        Reads the per-conversation transcript JSONL once and walks it from the
        tail, attaching user turns to slot ids in `sessions` order. Returns an
        empty mapping when the transcript file does not yet exist.
        """
        path = transcripts.transcript_path(self.instance_dir, conv)
        if not path.exists():
            return {}
        # Per spec, slots only exist on brains we've actually invoked. Pull
        # the slot map from `sessions` rows for context.
        conn = queue.connect(self.instance_dir)
        try:
            brain = self.config.default_brain
            rows = sessions.list_sessions_for_conversation(
                conn, channel=channel, conversation_id=conv, brain=brain
            )
        finally:
            conn.close()
        # Without slot rows we have no per-slot history; the classifier
        # bypasses cleanly (returns None) on an empty input.
        active_slots = sorted({r.slot for r in rows} | {0})
        active_slots = [s for s in active_slots if s < max_concurrent]
        if not active_slots:
            return {}
        # Approximation: slots share the transcript file. We use the last 3
        # user turns as a global summary, attributed to all known slots, so
        # the classifier can decide which is closest in topic. Per-slot
        # attribution would require slot stamps on each transcript line —
        # left as a follow-up (see spec Open Questions).
        events = transcripts.tail(path, lines=12)
        recent_user_turns = [ev.text for ev in events if ev.role == "user"][-3:]
        if not recent_user_turns:
            return {}
        joined = "\n- ".join(recent_user_turns)
        joined = "- " + joined
        return {s: joined for s in active_slots}

    def _build_global_transcript_context(self, event: queue.Event) -> str:
        """Render the last N transcript lines as a context block for parallel mode.

        Output is plain text (no markdown headers) so brains can splice it
        in front of the user input without breaking their own preamble.
        Returns "" when there's no transcript or the lines count is <= 0.
        """
        n = self.config.parallel.transcript_context_lines
        if n <= 0 or not event.conversation_id:
            return ""
        path = transcripts.transcript_path(self.instance_dir, event.conversation_id)
        if not path.exists():
            return ""
        # Drop the trailing user turn that the gateway just appended — it's
        # already present in event.content, no point sending it twice.
        events = transcripts.tail(path, lines=n + 1)
        if events and events[-1].role == "user" and events[-1].text == event.content:
            events = events[:-1]
        body = transcripts.render_priming_block(events)
        if not body:
            return ""
        return "[recent conversation context]\n" + body

    def _prefilter_slot_affinity(
        self,
        message: str,
        slot_summaries: dict[int, str],
    ) -> tuple[str, int | None]:
        """Deterministic pre-filter before the LLM classifier.

        Returns:
          ("unrelated", None)  — skip LLM, route as unrelated (new slot)
          ("related", slot_id) — currently unused; reserved for strong signal
          ("ambiguous", None)  — fall through to LLM

        See docs/specs/parallel-slots-smart-classifier.md.
        """
        if not slot_summaries:
            return ("ambiguous", None)

        msg_tokens = _tokenize(message)
        slot_tokens: dict[int, frozenset[str]] = {
            s: _tokenize(summary or "") for s, summary in slot_summaries.items()
        }

        if len(msg_tokens) < 2:
            # Short messages are noisy under Jaccard. Use token-or-entity
            # overlap against any slot; no overlap anywhere → unrelated.
            msg_entities = _extract_entities(message)
            signals: set[str] = set(msg_tokens) | {e.lower() for e in msg_entities}
            if not signals:
                return ("ambiguous", None)
            for slot_id, toks in slot_tokens.items():
                summary_lower = (slot_summaries.get(slot_id) or "").lower()
                for sig in signals:
                    if sig in toks or sig in summary_lower:
                        return ("ambiguous", None)
            return ("unrelated", None)

        max_j = 0.0
        for toks in slot_tokens.values():
            j = _jaccard(msg_tokens, toks)
            if j > max_j:
                max_j = j

        if max_j < _JACCARD_LOW:
            return ("unrelated", None)
        if max_j >= _JACCARD_HIGH:
            return ("ambiguous", None)

        # Mid-range Jaccard — entity check decides.
        msg_entities = _extract_entities(message)
        if not msg_entities:
            return ("ambiguous", None)
        all_slot_signals: set[str] = set()
        for summary in slot_summaries.values():
            text = summary or ""
            all_slot_signals.update(_extract_entities(text))
            all_slot_signals.update(_tokenize(text))
            all_slot_signals.add(text.lower())
        for ent in msg_entities:
            ent_lower = ent.lower()
            in_any = False
            for sig in all_slot_signals:
                if ent == sig or ent_lower == sig or ent_lower in sig:
                    in_any = True
                    break
            if not in_any:
                return ("unrelated", None)
        return ("ambiguous", None)

    def _classify_slot_affinity(
        self,
        event: queue.Event,
        slot_summaries: dict[int, str],
    ) -> int | None:
        """Ask openrouter whether `event` is related to a known slot.

        Returns the slot id when the model emits `related:<N>`, None
        otherwise. Best-effort: any HTTP / parsing error returns None
        (fall through to LRU free slot). Results are memoized for the
        configured TTL to dedupe repeat lookups.
        """
        verdict, _ = self._prefilter_slot_affinity(event.content, slot_summaries)
        if verdict == "unrelated":
            self.log(
                f"slot prefilter id={event.id} verdict=unrelated — skipping LLM",
                kind="slot_prefilter",
            )
            return None
        # "ambiguous" → continue to LLM. "related" path reserved for future
        # strong-signal short-circuit; today only "unrelated" fast-paths.

        cfg = self.config.parallel.classifier
        if cfg.backend != "openrouter":
            return None
        api_key = env_value(self.instance_dir, "OPENROUTER_API_KEY")
        if not api_key:
            return None
        slots_repr = "|".join(
            f"{s}:{(slot_summaries.get(s) or '').strip()[:200]}"
            for s in sorted(slot_summaries)
        )
        cache_key = (event.content[:400], slots_repr)
        now = time.time()
        cached = self._slot_classifier_cache.get(cache_key)
        if cached is not None and cached[0] > now:
            return _parse_slot_verdict(cached[1])
        prompt = self._slot_affinity_prompt(event.content, slot_summaries)
        body = {
            "model": cfg.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 16,
        }
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/matsei-ruka/juliuscaesar",
                "X-Title": "JuliusCaesar Gateway parallel-slots",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            self.log(f"slot classifier unreachable: {exc}", kind="slot_classifier_error")
            return None
        try:
            payload = json.loads(raw)
            text = (
                payload.get("choices", [{}])[0]
                .get("message", {})
                .get("content")
                or ""
            ).strip().lower()
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            self.log(f"slot classifier bad payload: {exc}", kind="slot_classifier_error")
            return None
        expires_at = now + max(1, cfg.cache_ttl_seconds)
        self._slot_classifier_cache[cache_key] = (expires_at, text)
        return _parse_slot_verdict(text)

    @staticmethod
    def _slot_affinity_prompt(
        message: str,
        slot_summaries: dict[int, str],
    ) -> str:
        """Build the classifier prompt for slot affinity.

        Output format: a single line — `related:<slot>` or `unrelated`. No
        explanation, no Markdown. Slot summaries are the last ~3 user turns
        per slot.
        """
        slot_blocks: list[str] = []
        for slot_id in sorted(slot_summaries):
            summary = slot_summaries[slot_id].strip() or "(no recent activity)"
            slot_blocks.append(f"--- slot {slot_id} ---\n{summary}")
        slots_text = "\n\n".join(slot_blocks)
        return (
            "You route a new chat message to a parallel work slot. Each slot "
            "is a long-lived thread of conversation. Your job: decide if the "
            "new message is a *direct continuation* of an existing slot, or a "
            "*new topic* that should run in a fresh parallel slot.\n\n"
            "DEFAULT TO `unrelated`. Only emit `related:<N>` when the new "
            "message is unmistakably continuing slot N's specific thread "
            "(answering a question slot N just asked, referencing a noun "
            "phrase from slot N's last turn, finishing a task slot N started).\n\n"
            "Treat as UNRELATED:\n"
            "  - A new question about a different person, system, file, or topic\n"
            "  - 'what about X', 'how about Y', 'altro?', 'cambio', 'next:', "
            "'btw', 'separately', 'aside from this' — these mark topic shifts\n"
            "  - A short imperative on a new subject ('check Daniel', "
            "'restart Florian', 'meteo dubai')\n"
            "  - A status query on a different entity than slot N's current focus\n"
            "  - A request that does not depend on any answer slot N is producing\n\n"
            "Treat as `related:N` ONLY when:\n"
            "  - The new message directly answers a question slot N just asked\n"
            "  - It references 'it', 'that', 'the X' where X was named in slot N\n"
            "  - It is a clear edit/follow-up ('and also do Y', 'one more thing on X')\n"
            "  - It corrects or clarifies slot N's last turn\n\n"
            "When in doubt → `unrelated`. False positives on `related` block "
            "parallelism; false positives on `unrelated` only cost a bit of "
            "context. Prefer the cheaper failure mode.\n\n"
            "Existing slot histories (last ~3 user turns each):\n\n"
            f"{slots_text}\n\n"
            "New message:\n"
            f"{message.strip()[:1500]}\n\n"
            "Reply with EXACTLY one token on a single line:\n"
            "  - `related:<N>` where <N> is the slot id the message continues\n"
            "  - `unrelated` if it starts a fresh topic\n"
            "No prose, no Markdown, no explanation."
        )

    def _lease_heartbeat(self, event_ids: list[int]) -> "_LeaseHeartbeat":
        """Context manager that renews the SQLite queue lease for ``event_ids``
        every ``lease_seconds / 3`` while the body runs.

        Without this, long brain invocations (>lease_seconds) trip
        ``requeue_expired`` mid-call, the row flips back to ``queued``, and
        another slot claims + dispatches the same event a second time. The
        observed symptom is duplicate replies on the channel.
        """

        return _LeaseHeartbeat(
            instance_dir=self.instance_dir,
            event_ids=list(event_ids),
            worker_id=self.worker_id,
            lease_seconds=self.config.lease_seconds,
            log=self.log,
        )

    def _dispatch_parallel(self, event: queue.Event) -> None:
        """Slot-aware dispatch for a single claimed event.

        Picks a slot. If the slot is busy, the event is reset to `queued`
        with a 1-second backoff so the next poll re-claims it once the slot
        frees. Otherwise spawns a daemon thread to invoke the brain on the
        chosen slot. Slot bookkeeping is released in `_run_in_slot` finally.
        """
        slot, should_queue = self._pick_slot(event)
        channel = router.channel_name(event)
        conv = event.conversation_id or ""
        key = (channel, conv)
        if should_queue:
            conn = queue.connect(self.instance_dir)
            try:
                queue.reset_running_to_queued(
                    conn,
                    event.id,
                    available_in_seconds=1,
                    expected_locked_by=self.worker_id,
                )
            finally:
                conn.close()
            self.log(
                f"event slot-queued id={event.id} slot={slot} "
                f"(retry in 1s)",
                event_id=event.id,
                kind="slot_queue",
            )
            return
        with self._slot_busy_lock:
            self._busy_slots.setdefault(key, set()).add(slot)
        thread = threading.Thread(
            target=self._run_in_slot,
            args=(event, slot, key),
            daemon=True,
            name=f"gateway-slot-{slot}",
        )
        with self._slot_busy_lock:
            self._slot_active_threads.add(thread)
        thread.start()

    def _run_in_slot(
        self,
        event: queue.Event,
        slot: int,
        key: tuple[str, str],
    ) -> None:
        """Worker-thread body: invoke the brain on `slot` and finalize the row."""
        from .brains import AdapterFailure

        try:
            self._persist_slot_in_meta(event, slot)
            with self._lease_heartbeat([event.id]):
                response = self.process_event(event, slot=slot)
            conn = queue.connect(self.instance_dir)
            try:
                try:
                    queue.complete(
                        conn,
                        event.id,
                        response=response,
                        expected_locked_by=self.worker_id,
                    )
                except KeyError:
                    self.log(
                        f"complete skipped (slot {slot}) id={event.id} "
                        f"reason=lease_lost worker={self.worker_id}"
                    )
            finally:
                conn.close()
            self._cancel_reengage_on_inbound_reply(event)
            self.log(f"event done (slot {slot}) id={event.id} source={event.source}")
        except AdapterFailure as failure:
            self._recovery.handle_adapter_failure(event, failure)
        except Exception as exc:  # noqa: BLE001
            conn = queue.connect(self.instance_dir)
            try:
                try:
                    queue.fail(
                        conn,
                        event.id,
                        error=str(exc)[:1000],
                        max_retries=self.config.max_retries,
                        expected_locked_by=self.worker_id,
                    )
                except KeyError:
                    self.log(
                        f"fail skipped (slot {slot}) id={event.id} "
                        f"reason=lease_lost worker={self.worker_id}"
                    )
            finally:
                conn.close()
            self.log(f"event errored (slot {slot}) id={event.id} error={exc}")
        finally:
            with self._slot_busy_lock:
                busy = self._busy_slots.get(key)
                if busy is not None:
                    busy.discard(slot)
                    if not busy:
                        self._busy_slots.pop(key, None)
                self._slot_active_threads.discard(threading.current_thread())

    def _apply_goal_lifecycle(self, event: queue.Event, batch_ids: list[int]) -> bool:
        """Set/clear the task-goal anchor (PR #65). Dispatch-loop thread only.

        Returns True iff the event was fully handled here (task_closed control
        event → no brain dispatch). task_assigned sets the goal and returns
        False so the event still dispatches to the brain (which works the task).
        """
        meta = decode_meta(event)
        kind = meta.get("kind")
        conversation_id = event.conversation_id or ""

        if kind == "task_closed":
            goal_cache.clear(self.instance_dir, conversation_id, meta.get("task_id"))
            self.log(
                f"goal cleared conv={conversation_id or '-'} prev_task={meta.get('task_id')}",
                kind="goal_cleared",
            )
            conn = queue.connect(self.instance_dir)
            try:
                for eid in batch_ids:
                    try:
                        queue.complete(
                            conn, eid, response="(task closed)", expected_locked_by=self.worker_id
                        )
                    except KeyError:
                        self.log(
                            f"complete skipped id={eid} reason=lease_lost worker={self.worker_id}"
                        )
            finally:
                conn.close()
            return True

        if kind == "task_assigned" and conversation_id:
            text = goal_cache.format_goal(meta)
            if text and goal_cache.set(
                self.instance_dir, conversation_id, str(meta.get("task_id") or ""), text
            ):
                self.log(
                    f"goal set conv={conversation_id} task={meta.get('task_id')} "
                    f"text_chars={len(text)}",
                    kind="goal_set",
                )
        return False

    def process_event(self, event: queue.Event, *, slot: int = 0) -> str:
        monotonic_start = time.monotonic()
        meta = decode_meta(event)
        if meta.get("deliver_only"):
            response = event.content
            self._deliver_response(event.source, response, meta)
            return response

        channel = router.channel_name(event)
        coalesced = bool(meta.get("coalesced_ids"))
        if not coalesced:
            event, meta = self._apply_inline_override(event, meta)

            slash = overrides.parse_slash_command(event.content)
            if slash is not None:
                return self._handle_slash(slash, event, meta, channel)

        sticky = self._resolve_sticky(event, channel)
        triage, triage_rejected = self._maybe_triage(event, sticky)
        if triage_rejected:
            return self._notify_triage_rejection(event, meta, channel)
        selection = router.route(
            event,
            cfg=self.config,
            sticky=sticky,
            triage=triage,
            confidence_threshold=self.config.triage.confidence_threshold,
            fallback_brain=self.config.triage.fallback_brain,
        )
        brain, model = selection.brain, selection.model
        if brain == "openrouter" and not (
            selection.reason == "triage"
            and triage is not None
            and triage.full_spec() == self.config.triage.unsafe_fallback_brain
        ):
            raise ValueError("openrouter brain is only supported for triage_unsafe_fallback_brain")
        if (
            meta.get("image_path")
            and not self.config.pin_to_default_brain
            and not capabilities.supports_images(brain)
        ):
            vision_brain = self._select_vision_brain()
            if vision_brain and vision_brain != brain:
                self.log(
                    f"vision route id={event.id} forcing brain={vision_brain} "
                    f"(was {brain}) reason=image_path"
                )
                brain, model = vision_brain, None
        brain, model = self._triage_capacity_guard(
            event=event, channel=channel, brain=brain, model=model
        )
        self.log(
            f"route id={event.id} channel={channel} brain={brain} "
            f"model={model or '-'} reason={selection.reason}"
        )

        resume_session = self._resume_id(channel, event.conversation_id, brain, slot=slot)
        # Phase 2 — supervisor card Background: when the session bound to
        # this conversation has been demoted to background, force a fresh
        # brain session for the new inbound so the backgrounded native
        # session keeps running uninterrupted to completion.
        if (
            resume_session
            and event.conversation_id
            and self.config.actions.enabled
            and actions_registry.has_backgrounded_for_conversation(event.conversation_id)
        ):
            self.log(
                f"session resume bypass id={event.id} conv={event.conversation_id} "
                f"reason=backgrounded_active — spawning fresh primary",
                kind="action_routing",
            )
            resume_session = None
        self.log(
            f"session resume id={event.id} conv={event.conversation_id or '-'} "
            f"brain={brain} slot={slot} session={resume_session or 'none'}"
        )
        brain, model, resume_session = self._apply_routing_pressure(
            event=event,
            channel=channel,
            brain=brain,
            model=model,
            slot=slot,
            resume_session=resume_session,
        )
        self.log(
            f"dispatch begin id={event.id} brain={brain} model={model or '-'} "
            f"slot={slot} resume={'yes' if resume_session else 'no'}"
        )
        # Parallel mode: prepend the last N global transcript lines so each
        # slot stays loosely aware of activity in sibling slots. Off by default
        # (N=1) — the brain's own priming path handles serial resume.
        if (
            self.config.parallel.max_concurrent > 1
            and event.conversation_id
            and not coalesced
        ):
            extra_ctx = self._build_global_transcript_context(event)
            if extra_ctx:
                event = dataclasses.replace(event, content=extra_ctx + "\n\n" + event.content)

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

        # Phase 2 — Background interception: when the brain subprocess was
        # demoted to background mid-flight, the reply must not flow through
        # the normal delivery path (the chat is already owned by a fresh
        # primary session). Re-render as a "Background done" completion
        # card targeting the original supervisor message and skip the
        # session-row write (the backgrounded native session is one-shot).
        if result.action_role == "backgrounded":
            self._handle_background_completion(
                event=event,
                brain=brain,
                model=model,
                result=result,
                meta=meta,
                channel=channel,
                monotonic_start=monotonic_start,
                slot=slot,
            )
            return result.response or ""

        # When Codex resumes (no new rollout file written), capture_session_id
        # returns None even though the dispatch ran inside `resume_session`.
        # Use the resumed id as the effective session for footer + bookkeeping
        # so the table's updated_at reflects activity and the footer doesn't
        # falsely advertise "no session".
        effective_session_id = result.session_id or resume_session
        if effective_session_id:
            self._record_session(
                channel,
                event.conversation_id,
                brain,
                effective_session_id,
                slot=slot,
            )

        self._record_context_usage(
            event=event,
            channel=channel,
            brain=brain,
            model=model,
            slot=slot,
            result=result,
            session_id=effective_session_id,
        )

        # Sticky brain is only set by an explicit user action: `/brain X` slash
        # or `[brain] ...` inline prefix. Triage runs every message otherwise,
        # so a "hi" followed immediately by "compare three providers" still
        # routes the second message to the appropriate brain.

        raw_response = result.response or ""
        parsed = parse_brain_output(raw_response, event_source=event.source)
        pushed_via_marker = push_marker_sent(result.push_marker_path)
        if parsed.parse_error:
            delivery_note = (
                "using recovered envelope message"
                if parsed.parse_error == RECOVERED_ENVELOPE_ERROR
                else "treating raw stdout as message"
            )
            self.log(
                f"dispatch parse-error id={event.id} brain={brain} — "
                f"{parsed.parse_error}; {delivery_note}",
                kind="brain_output_parse_error",
            )

        meta.setdefault("delivery_channel", channel)
        response_text = parsed.message
        if parsed.push_message_sent or pushed_via_marker:
            reason = (
                "canonical sender marker detected"
                if pushed_via_marker and not parsed.push_message_sent
                else "brain reports message already pushed"
            )
            self.log(
                f"dispatch push-handled id={event.id} brain={brain} — "
                f"{reason}, skipping channel delivery"
            )
            if parsed.message:
                self._log_outbound_transcript(event, parsed.message, meta, channel)
        elif parsed.message:
            footer = reply_footer.render_footer(
                self.config.reply_footer,
                brain=brain,
                model=model,
                session_id=effective_session_id,
                elapsed_seconds=_elapsed_seconds(event, monotonic_start),
                slot=slot,
                max_concurrent=self.config.parallel.max_concurrent,
            )
            message_out = parsed.message + ("\n\n" + footer if footer else "")
            response_text = message_out
            if meta.get("was_voice"):
                self._render_voice_reply(parsed.message, meta)
            self._deliver_response(channel, message_out, meta)
            self._log_outbound_transcript(event, message_out, meta, channel)
        else:
            self.log(
                f"dispatch silent id={event.id} brain={brain} — "
                "brain produced no text, skipping delivery + transcript log"
            )
        if self._company_reporter is not None:
            try:
                meta_with_brain = {**meta, "brain": brain}
                self._company_reporter.on_conversation(event, parsed.message, meta_with_brain)
            except Exception as exc:  # noqa: BLE001
                self.log(f"company on_conversation error: {exc}", kind="company_error")
        return response_text

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

    def _handle_background_completion(
        self,
        *,
        event: queue.Event,
        brain: str,
        model: str | None,
        result: Any,
        meta: dict[str, Any],
        channel: str,
        monotonic_start: float,
        slot: int,
    ) -> None:
        """Render + deliver a "Background done" card for a demoted session.

        Bypasses the normal reply path:
          - DOES NOT call ``_deliver_response`` (would race with the fresh
            primary session that now owns the chat).
          - DOES NOT record the session_id (the backgrounded native session
            is single-use; no resume of it ever happens).
          - DOES write the outbound text to the conversation transcript so
            the new primary can see what the backgrounded sibling produced.
        """
        parsed = parse_brain_output(result.response or "", event_source=event.source)
        body = (parsed.message or "").strip()
        buffered = list(result.action_buffered_tool_messages or ())
        # Buffered tool messages were captured mid-task while suppression was
        # on; prepend them so the operator sees what the brain "would have"
        # sent during the run.
        if buffered:
            buffered_block = "\n\n".join(b.strip() for b in buffered if b and b.strip())
            if buffered_block:
                body = buffered_block + (("\n\n" + body) if body else "")

        elapsed_total = max(
            0.0,
            time.time() - float(result.action_started_at or monotonic_start),
        )
        mm = int(elapsed_total) // 60
        ss = int(elapsed_total) % 60
        duration_str = f"{mm:02d}:{ss:02d}"
        completion_header = f"🔄 Background done · {duration_str}:"
        completion_text = (
            f"{completion_header}\n\n{body}" if body else completion_header
        )

        chat_id = (
            result.action_bg_chat_id
            or meta.get("chat_id")
            or meta.get("notify_chat_id")
            or event.conversation_id
            or ""
        )
        # Skip the normal Telegram out-path entirely. Build a minimal meta so
        # send_text writes a fresh message to the same chat without threading
        # it as a reply to the inbound that triggered the now-stale primary.
        send_meta: dict[str, Any] = {"chat_id": str(chat_id)}
        if meta.get("message_thread_id"):
            send_meta["message_thread_id"] = meta["message_thread_id"]

        token = env_value(self.instance_dir, "TELEGRAM_BOT_TOKEN")
        if token and chat_id and completion_text.strip():
            try:
                telegram_send_text(
                    instance_dir=self.instance_dir,
                    token=token,
                    response=completion_text,
                    meta=send_meta,
                    log=self.log,
                )
            except Exception as exc:  # noqa: BLE001
                self.log(
                    f"background completion send failed event={event.id}: {exc}",
                    kind="action_background_send_error",
                )

        # Edit the original supervisor card: drop the keyboard, append the
        # "Done at HH:MM:SS UTC · MM:SS" line so the historical card shows
        # the final state inline.
        bg_msg_id = result.action_bg_supervisor_msg_id
        if token and chat_id and bg_msg_id:
            done_suffix = self._background_done_suffix(duration_str)
            original = result.action_card_text or ""
            new_text = (
                original.rstrip() + "\n\n" + done_suffix if original else done_suffix
            )
            self._edit_supervisor_card_after_background_done(
                token=token,
                chat_id=str(chat_id),
                message_id=int(bg_msg_id),
                text=new_text,
            )

        # Persist the outbound to the transcript so any follow-up turn in the
        # fresh primary has visibility into what the background sibling said.
        if body:
            self._log_outbound_transcript(event, body, meta, channel)

        # Audit completion so jc-doctor can correlate with background records.
        try:
            from . import actions as _actions
            _actions.audit_background_done(
                self.instance_dir,
                result.action_session_id or "",
                str(chat_id),
                duration_s=elapsed_total,
                reason="done",
            )
        except Exception:  # noqa: BLE001
            pass

        self.log(
            f"background completion delivered event={event.id} chat={chat_id} "
            f"duration={duration_str} buffered={len(buffered)} body_chars={len(body)}",
            kind="action_background_completion",
        )

    @staticmethod
    def _background_done_suffix(duration_str: str) -> str:
        """Build the ``🔄 Done at HH:MM:SS UTC · MM:SS`` trailing card line."""
        hhmmss = datetime.now(timezone.utc).strftime("%H:%M:%S")
        return f"🔄 Done at {hhmmss} UTC · {duration_str}"

    def _edit_supervisor_card_after_background_done(
        self,
        *,
        token: str,
        chat_id: str,
        message_id: int,
        text: str,
    ) -> None:
        """Drop the keyboard and replace the card text with the Done state."""
        from .channels._http import http_json
        from .format import to_markdown_v2

        payload = {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "text": to_markdown_v2(text),
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
            "reply_markup": json.dumps({"inline_keyboard": []}),
        }
        try:
            data = http_json(
                f"https://api.telegram.org/bot{token}/editMessageText",
                data=payload,
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(
                f"background completion card edit failed: {exc}",
                kind="action_background_edit_error",
            )
            return
        if data.get("ok"):
            return
        description = str(data.get("description") or "").lower()
        if "not modified" in description:
            return
        if "parse" in description or "entit" in description:
            try:
                http_json(
                    f"https://api.telegram.org/bot{token}/editMessageText",
                    data={
                        "chat_id": str(chat_id),
                        "message_id": int(message_id),
                        "text": text,
                        "disable_web_page_preview": True,
                        "reply_markup": json.dumps({"inline_keyboard": []}),
                    },
                    timeout=10,
                )
            except Exception as exc:  # noqa: BLE001
                self.log(
                    f"background completion card plain-fallback failed: {exc}",
                    kind="action_background_edit_error",
                )
            return
        self.log(
            f"background completion card edit not ok: {data}",
            kind="action_background_edit_error",
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
        chat_action = telegram_chat_action_for_meta(meta)

        def loop() -> None:
            try:
                def send_action(chat_id: str, message_thread_id: int | None) -> None:
                    telegram_channel.send_typing(
                        chat_id,
                        message_thread_id=message_thread_id,
                        action=chat_action,
                    )

                typing_loop(
                    send_action,
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
        if slash.kind == "compact":
            return self._handle_compact(event, meta, channel)
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

    def _handle_compact(
        self,
        event: queue.Event,
        meta: dict[str, Any],
        channel: str,
    ) -> str:
        if not event.conversation_id:
            reply = "Nothing to compact — no active conversation."
            self._deliver_response(channel, reply, dict(meta, delivery_channel=channel))
            return reply
        busy = set(self._busy_slots.get((channel, event.conversation_id), set()))
        result = compaction.compact_conversation(
            self,
            channel=channel,
            conversation_id=event.conversation_id,
            trigger=notify.TRIGGER_COMPACT,
            busy_slots=busy,
        )
        reply = result.report
        out_meta = dict(meta)
        out_meta.setdefault("delivery_channel", channel)
        self._deliver_response(channel, reply, out_meta)
        self.log(
            f"slash command id={event.id} kind=compact trigger={notify.TRIGGER_COMPACT} "
            f"channel={channel} conversation_id={event.conversation_id} "
            f"rotated={len(result.compacted)} queued={len(result.queued)}",
            event_id=event.id,
            kind="context_compaction",
            trigger=notify.TRIGGER_COMPACT,
            channel=channel,
            conversation_id=event.conversation_id,
            slots_rotated=len(result.compacted),
            slots_queued=len(result.queued),
            compacted_slots=[
                {
                    "owner_kind": "gateway",
                    "owner_key": compaction.owner_key(channel, event.conversation_id, item.brain, item.slot),
                    "brain": item.brain,
                    "slot": item.slot,
                    "tokens_before": item.tokens_before,
                    "tokens_after": item.tokens_after,
                    "method": item.method,
                }
                for item in result.compacted
            ],
            queued_slots=[
                {
                    "owner_kind": "gateway",
                    "owner_key": compaction.owner_key(channel, event.conversation_id, ref.brain, ref.slot),
                    "brain": ref.brain,
                    "slot": ref.slot,
                    "session_id_prefix": ref.session_id[:8],
                }
                for ref in result.queued
            ],
        )
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

    # --- §11 routing pressure + §8 telemetry persistence -----------------

    def _resolve_context_profile(self, brain: str, model: str | None):
        """Map a routed (brain, model) to a registry + standard profile.

        Returns (registry, profile|None). The profile is None when the model
        cannot be resolved to a known capacity profile — the guard then
        dispatches unchanged rather than guessing capacity (§5.2, §9).
        """
        registry = self.config.session_lifecycle.registry()
        if not model:
            return registry, None
        profile = registry.for_model(model)
        if profile is None and brain == "claude" and not model.startswith("claude-"):
            # Brain specs carry short aliases ("sonnet-4-6" from "claude:sonnet-4-6")
            # but built-in profiles use canonical ids ("claude-sonnet-4-6"). Try the
            # prefixed form as a fallback so operator-defined model names (e.g. "small")
            # resolve on exact match and don't get mangled.
            profile = registry.for_model(f"claude-{model}")
        return registry, profile

    @staticmethod
    def _model_family(model: str | None) -> str:
        text = (model or "").strip().lower()
        if text.startswith("claude"):
            return "claude"
        if text.startswith(("gpt-", "o", "codex")):
            return "codex"
        if text.startswith("gemini"):
            return "gemini"
        return text.split(":", 1)[0] if ":" in text else ""

    def _rotate_session_for_pressure(
        self,
        *,
        channel: str,
        conversation_id: str,
        brain: str,
        slot: int,
        resume_session: str,
        kind: str,
        event_id: int,
        reason: str,
    ) -> None:
        conn = queue.connect(self.instance_dir)
        try:
            rotated = compaction.rotate_slot(
                conn,
                channel=channel,
                conversation_id=conversation_id,
                brain=brain,
                slot=slot,
                expected_session_id=resume_session,
            )
        finally:
            conn.close()
        if rotated is not None:
            self.log(
                f"{kind} id={event_id} channel={channel} conversation_id={conversation_id} "
                f"brain={brain} slot={slot} reason={reason}",
                event_id=event_id,
                kind=kind,
            )

    def _fail_routing_pressure(
        self,
        *,
        event: queue.Event,
        channel: str,
        message: str,
    ) -> None:
        meta = decode_meta(event)
        meta.setdefault("delivery_channel", channel)
        self._deliver_response(channel, message, meta)
        conn = queue.connect(self.instance_dir)
        try:
            try:
                queue.fail(
                    conn,
                    event.id,
                    error=message,
                    max_retries=0,
                    expected_locked_by=self.worker_id,
                )
            except KeyError:
                pass
        finally:
            conn.close()
        raise RuntimeError(message)

    # Triage-time safety: brains whose ceiling is 200K (sonnet/haiku) must not
    # be chosen for a conversation whose tracked context already exceeds the
    # safe input threshold. Override to claude:opus (which carries the 1M
    # extended profile).
    # Format: (brain, model_prefix, threshold_tokens, target_brain_spec)
    # model_prefix matches the start of the model string (e.g. "sonnet" matches
    # "sonnet", "sonnet-4-6", etc.). brain is the bare brain name ("claude").
    _TRIAGE_CAPACITY_OVERRIDES: tuple[tuple[str, str, int, str], ...] = (
        ("claude", "sonnet", 170_000, "claude:opus"),
        ("claude", "haiku", 170_000, "claude:opus"),
    )

    def _triage_capacity_guard(
        self,
        *,
        event: queue.Event,
        channel: str,
        brain: str,
        model: str | None,
    ) -> tuple[str, str | None]:
        if not event.conversation_id:
            return brain, model
        rule = next(
            (
                r
                for r in self._TRIAGE_CAPACITY_OVERRIDES
                if r[0] == brain and (model or "").startswith(r[1])
            ),
            None,
        )
        if rule is None:
            return brain, model
        _, _prefix, threshold, target_spec = rule
        target_brain_name, _, target_model = target_spec.partition(":")

        conn = queue.connect(self.instance_dir)
        try:
            telemetry.init_db(conn)
            row = conn.execute(
                "SELECT MAX(effective_input_tokens) FROM session_lifecycle "
                "WHERE owner_key LIKE ?",
                (f"gateway:{channel}:{event.conversation_id}:%",),
            ).fetchone()
        finally:
            conn.close()
        max_ctx = int(row[0]) if row and row[0] else 0
        if max_ctx <= threshold:
            return brain, model
        self.log(
            f"triage_capacity_guard id={event.id} from_brain={brain} "
            f"to_brain={target_brain_name} max_effective={max_ctx} "
            f"threshold={threshold}",
            event_id=event.id,
            kind="triage_capacity_guard",
            channel=channel,
            conversation_id=event.conversation_id,
            from_brain=brain,
            to_brain=target_brain_name,
            max_effective_input_tokens=max_ctx,
            threshold_tokens=threshold,
        )
        return target_brain_name, target_model or None

    def _apply_routing_pressure(
        self,
        *,
        event: queue.Event,
        channel: str,
        brain: str,
        model: str | None,
        slot: int,
        resume_session: str | None,
    ) -> tuple[str, str | None, str | None]:
        """§11 pre-dispatch size gate — upgrade to a larger-capacity profile
        when the selected one can no longer fit the projected turn. Gated by
        `session_lifecycle.enabled`; a no-op when disabled or unresolvable."""
        lc = self.config.session_lifecycle
        if not lc.enabled or not event.conversation_id:
            return brain, model, resume_session
        registry, selected = self._resolve_context_profile(brain, model)
        if selected is None:
            return brain, model, resume_session
        owner = compaction.owner_key(channel, event.conversation_id, brain, slot)
        conn = queue.connect(self.instance_dir)
        try:
            tel = telemetry.get_telemetry(conn, owner_key=owner)
        finally:
            conn.close()
        last_eff = (tel.effective_input_tokens or 0) if tel else 0
        required = routing.required_context(
            last_effective_input=last_eff,
            estimated_new_prompt=_estimate_prompt_tokens(event.content),
            reserves=lc.reserves,
        )
        ceiling = profiles.session_ceiling(registry, model=selected.model, selected=selected)
        larger = [
            p
            for p in registry.enabled_for_model(selected.model)
            if p.input_capacity_tokens > selected.input_capacity_tokens
        ]
        decision = routing.evaluate_pressure(
            selected_profile=selected,
            ceiling=ceiling,
            required=required,
            current_context=last_eff,
            thresholds=lc.thresholds,
            resumed=bool(resume_session),
            larger_profiles=larger,
            usage_known=bool(tel and tel.effective_input_tokens),
        )
        if decision.action == routing.UPGRADE and decision.upgrade_profile is not None:
            up = decision.upgrade_profile
            model_family = self._model_family(up.model)
            if model_family and model_family != brain.split(":", 1)[0]:
                decision = routing.GuardDecision(
                    routing.ROTATE,
                    "upgrade profile crosses brain family; rotating instead",
                    decision.routing_pressure,
                    decision.lifecycle_pressure,
                    decision.selected_profile,
                )
            else:
                session_prefix = resume_session[:8] if resume_session else None
                self.log(
                    f"context_capacity_upgrade id={event.id} brain={brain} "
                    f"from_model={selected.model} to_model={up.model} "
                    f"from_profile={selected.key} to_profile={up.key} "
                    f"pressure={decision.routing_pressure:.2f}",
                    event_id=event.id,
                    kind="context_capacity_upgrade",
                    owner_kind="gateway",
                    owner_key=owner,
                    brain=brain,
                    channel=channel,
                    conversation_id=event.conversation_id,
                    slot=slot,
                    session_id_prefix=session_prefix,
                    effective_input_tokens=last_eff,
                    from_model=selected.model,
                    to_model=up.model,
                    from_profile=selected.key,
                    to_profile=up.key,
                    selected_capacity_tokens=selected.input_capacity_tokens,
                    session_ceiling_capacity_tokens=ceiling.input_capacity_tokens if ceiling else None,
                    pressure=decision.routing_pressure,
                    routing_pressure=decision.routing_pressure,
                    lifecycle_pressure=decision.lifecycle_pressure,
                    reason=decision.reason,
                )
                return brain, up.model, resume_session
        if decision.action in (routing.ROTATE, routing.EMERGENCY_ROTATE) and resume_session:
            self._rotate_session_for_pressure(
                channel=channel,
                conversation_id=event.conversation_id,
                brain=brain,
                slot=slot,
                resume_session=resume_session,
                kind=(
                    "context_emergency_rotate"
                    if decision.action == routing.EMERGENCY_ROTATE
                    else "context_rotate"
                ),
                event_id=event.id,
                reason=decision.reason,
            )
            return brain, model, None
        if decision.action == routing.FAIL:
            self._fail_routing_pressure(
                event=event,
                channel=channel,
                message=(
                    "Unable to route this turn safely: the active session cannot fit the "
                    "next dispatch and no compatible capacity upgrade is available."
                ),
            )
        return brain, model, resume_session

    def _record_context_usage(
        self,
        *,
        event: queue.Event,
        channel: str,
        brain: str,
        model: str | None,
        slot: int,
        result: Any,
        session_id: str | None = None,
    ) -> None:
        """§8 persist the turn's context usage. Gated by session_lifecycle."""
        lc = self.config.session_lifecycle
        if not lc.enabled or not event.conversation_id:
            return
        owner = compaction.owner_key(channel, event.conversation_id, brain, slot)
        raw = getattr(result, "usage", None)
        if isinstance(raw, dict) and raw:
            usage = telemetry.ContextUsage.from_anthropic_usage(raw, source="api")
        else:
            usage = telemetry.ContextUsage(
                input_tokens=None,
                cache_creation_input_tokens=None,
                cache_read_input_tokens=None,
                output_tokens=None,
                effective_input_tokens=None,
                source="estimate",
                measured_at=telemetry.now_iso(),
            )
        _, selected = self._resolve_context_profile(brain, model)
        conn = queue.connect(self.instance_dir)
        try:
            tel = telemetry.record_usage(
                conn,
                owner_key=owner,
                brain=brain,
                usage=usage,
                model=model,
                context_profile=selected.key if selected else None,
            )
        finally:
            conn.close()
        lifecycle_p: float | None = None
        ceiling = None
        if tel.effective_input_tokens is not None and selected is not None:
            registry, _ = self._resolve_context_profile(brain, model)
            ceiling = profiles.session_ceiling(registry, model=selected.model, selected=selected)
            lifecycle_p = routing.lifecycle_pressure(tel.effective_input_tokens, ceiling)
        session_prefix = session_id[:8] if session_id else None
        self.log(
            f"context_usage_updated owner={owner} brain={brain} slot={slot} "
            f"model={model or '-'} effective_input_tokens={tel.effective_input_tokens} "
            f"source={tel.usage_source} lifecycle_pressure="
            f"{f'{lifecycle_p:.2f}' if lifecycle_p is not None else '-'} turn={tel.turn_count}",
            event_id=event.id,
            kind="context_usage_updated",
            owner_kind="gateway",
            owner_key=owner,
            brain=brain,
            channel=channel,
            conversation_id=event.conversation_id,
            slot=slot,
            session_id_prefix=session_prefix,
            model=model,
            context_profile=selected.key if selected else None,
            effective_input_tokens=tel.effective_input_tokens,
            usage_source=tel.usage_source,
            selected_capacity_tokens=selected.input_capacity_tokens if selected else None,
            session_ceiling_capacity_tokens=ceiling.input_capacity_tokens if ceiling else None,
            lifecycle_pressure=lifecycle_p,
            turn_count=tel.turn_count,
        )
