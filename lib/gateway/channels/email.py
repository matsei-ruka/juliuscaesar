"""Email channel — gateway integration.

Polls IMAP via `lib.channels.email.EmailChannelAdapter`, dispatches inbound
messages through `email_dispatcher.dispatch_messages` (allowed → enqueue,
unknown → Telegram notify + pending, blocked → silent drop), and sends
outbound responses via SMTP using meta supplied by the runtime.

The internal poller can be disabled by setting `imap.poll_interval: 0` in
gateway.yaml — typical deployments drive polling externally via the
`heartbeat/fetch/email-poll.sh` cron script and use this channel only for
outbound delivery.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import yaml

from ..config import ChannelConfig, env_value
from .base import EnqueueFn, LogFn
from . import email_dispatcher


def _load_email_config(instance_dir: Path) -> dict[str, Any]:
    """Read the raw `channels.email` block from `ops/gateway.yaml`."""
    path = instance_dir / "ops" / "gateway.yaml"
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    channels = data.get("channels") or {}
    email_cfg = channels.get("email") or {}
    return email_cfg if isinstance(email_cfg, dict) else {}


def _load_env(instance_dir: Path) -> dict[str, str]:
    """Collect IMAP/SMTP env values for the adapter."""
    keys = ("IMAP_HOST", "IMAP_PORT", "IMAP_USER", "IMAP_PASSWORD", "SMTP_PORT")
    return {k: env_value(instance_dir, k) for k in keys}


class EmailChannel:
    name = "email"

    def __init__(self, instance_dir: Path, cfg: ChannelConfig, log: LogFn):
        self.instance_dir = instance_dir
        self.cfg = cfg
        self.log = log
        self._adapter = None
        self._adapter_error: str | None = None

    def _build_adapter(self):
        """Lazy-construct the EmailChannelAdapter. Cached after first call."""
        if self._adapter is not None or self._adapter_error is not None:
            return self._adapter
        try:
            from channels.email import EmailChannelAdapter  # type: ignore
        except ImportError as exc:
            self._adapter_error = f"import: {exc}"
            self.log(f"email channel disabled — adapter import failed: {exc}")
            return None
        cfg_raw = _load_email_config(self.instance_dir)
        env = _load_env(self.instance_dir)
        try:
            self._adapter = EmailChannelAdapter(
                instance_dir=self.instance_dir,
                config=cfg_raw,
                env=env,
            )
        except Exception as exc:  # noqa: BLE001
            self._adapter_error = f"init: {exc}"
            self.log(f"email channel disabled — adapter init failed: {exc}")
            return None
        return self._adapter

    def ready(self) -> bool:
        cfg_raw = _load_email_config(self.instance_dir)
        env = _load_env(self.instance_dir)
        host = (cfg_raw.get("imap") or {}).get("host") or env.get("IMAP_HOST")
        user = (cfg_raw.get("imap") or {}).get("user") or env.get("IMAP_USER")
        password = env.get("IMAP_PASSWORD") or (cfg_raw.get("imap") or {}).get("password")
        return bool(host and user and password)

    def _poll_interval(self) -> int:
        """0 disables internal polling; external cron drives fetches instead."""
        cfg_raw = _load_email_config(self.instance_dir)
        imap_cfg = cfg_raw.get("imap") or {}
        try:
            return int(imap_cfg.get("poll_interval", 0))
        except (TypeError, ValueError):
            return 0

    def run(self, enqueue: EnqueueFn, should_stop: Callable[[], bool]) -> None:
        if not self.ready():
            self.log("email channel disabled — IMAP credentials missing")
            return
        interval = self._poll_interval()
        if interval <= 0:
            self.log("email channel: internal poller disabled (poll_interval<=0)")
            # Block until shutdown so the runtime treats the channel as live.
            while not should_stop():
                time.sleep(1)
            return
        self.log(f"email channel poller started interval={interval}s")
        while not should_stop():
            try:
                self._poll_once(enqueue)
            except Exception as exc:  # noqa: BLE001
                self.log(f"email poll error: {exc}")
            for _ in range(interval):
                if should_stop():
                    break
                time.sleep(1)
        self.log("email channel poller stopped")

    def _poll_once(self, enqueue: EnqueueFn) -> None:
        adapter = self._build_adapter()
        if adapter is None:
            return
        cfg_raw = _load_email_config(self.instance_dir)
        messages = adapter.fetch_new_messages()
        if not messages:
            return
        result = email_dispatcher.dispatch_messages(
            instance_dir=self.instance_dir,
            messages=messages,
            enqueue=enqueue,
            cfg=cfg_raw,
            log=self.log,
        )
        self.log(
            f"email poll: dispatched={result.dispatched} "
            f"pending={result.pending} blocked={result.blocked}"
        )

    def send(self, response: str, meta: dict[str, Any]) -> str | None:
        """Send a reply via SMTP. Requires meta keys set by the inbound enqueue."""
        if not response.strip():
            return None
        recipient = meta.get("email_to") or meta.get("recipient") or meta.get("sender")
        if not recipient:
            self.log("email send skipped — no recipient in meta")
            return None
        adapter = self._build_adapter()
        if adapter is None:
            return None
        subject = meta.get("email_subject") or meta.get("subject") or "(no subject)"
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        in_reply_to = meta.get("email_message_id") or meta.get("in_reply_to")
        references = meta.get("email_references") or meta.get("references") or []
        if isinstance(references, str):
            references = [references]
        if in_reply_to and in_reply_to not in references:
            references = list(references) + [in_reply_to]
        try:
            return adapter.send_reply(
                conversation_id=meta.get("conversation_id") or "",
                recipient=str(recipient),
                subject=str(subject),
                body=response,
                in_reply_to=in_reply_to,
                references=list(references) if references else None,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"email send failed: {exc}")
            return None
