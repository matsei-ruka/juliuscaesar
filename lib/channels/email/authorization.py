"""Sender authorization: allowlist/blocklist with hot-reload."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Optional

import yaml

__all__ = ["SenderAuthorizer", "update_sender_list"]


def update_sender_list(config_path: Path, list_name: str, sender_email: str) -> None:
    """Update sender allowlist or blocklist in gateway.yaml (atomic, idempotent).

    Used by jc-chats approve/deny CLI to manage senders atomically.
    """
    if not config_path.exists():
        return

    config = yaml.safe_load(config_path.read_text()) or {}
    channels = config.get("channels", {})
    email_cfg = channels.get("email", {})
    senders_cfg = email_cfg.get("senders", {})

    senders_list = senders_cfg.get(list_name, [])
    if not isinstance(senders_list, list):
        senders_list = []

    sender = sender_email.lower().strip()
    if sender not in senders_list:
        senders_list.append(sender)
        senders_cfg[list_name] = sorted(senders_list)
        email_cfg["senders"] = senders_cfg
        channels["email"] = email_cfg
        config["channels"] = channels

        content = yaml.dump(config, default_flow_style=False)
        fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, prefix=".gateway.", suffix=".yaml")
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp_path, config_path)
        except Exception:
            os.close(fd)
            os.unlink(tmp_path)
            raise


class SenderAuthorizer:
    """Manages sender allowlist/blocklist with mtime-based hot-reload."""

    def __init__(
        self,
        config_path: Path,
        check_interval: float = 5.0,
    ):
        """
        Args:
            config_path: Path to ops/gateway.yaml
            check_interval: Seconds between mtime checks (default 5s)
        """
        self.config_path = Path(config_path)
        self.check_interval = check_interval
        self.last_mtime = 0.0
        self.allowed: set[str] = set()
        self.blocked: set[str] = set()
        self.last_check = 0.0
        self._reload()

    def _reload(self) -> None:
        """Load/reload config from gateway.yaml."""
        try:
            mtime = self.config_path.stat().st_mtime
        except FileNotFoundError:
            mtime = 0.0

        if mtime <= self.last_mtime:
            return

        self.last_mtime = mtime
        self.allowed = set()
        self.blocked = set()

        if not self.config_path.exists():
            return

        try:
            config = yaml.safe_load(self.config_path.read_text()) or {}
            email_cfg = config.get("channels", {}).get("email", {})
            senders_cfg = email_cfg.get("senders", {})

            allowed = senders_cfg.get("allowed", [])
            if isinstance(allowed, list):
                self.allowed = {s.lower() for s in allowed if s}

            blocked = senders_cfg.get("blocklist", [])
            if isinstance(blocked, list):
                self.blocked = {s.lower() for s in blocked if s}
        except Exception:
            # YAML parse error; keep stale config
            pass

    def check(self, sender_email: str) -> str:
        """Classify sender: 'allowed' | 'blocked' | 'unknown'.

        Hot-reloads config on mtime change (~5s polling).
        """
        now = time.time()
        if now - self.last_check >= self.check_interval:
            self._reload()
            self.last_check = now

        sender = sender_email.lower().strip()

        if sender in self.blocked:
            return "blocked"
        if sender in self.allowed:
            return "allowed"
        return "unknown"
