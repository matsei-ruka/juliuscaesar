"""Sender authorization: allowlist/blocklist with hot-reload."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import yaml

__all__ = ["SenderAuthorizer"]


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

    def add_allowed(self, sender_email: str) -> None:
        """Add sender to allowlist in gateway.yaml."""
        self._update_list("allowed", sender_email)

    def add_blocked(self, sender_email: str) -> None:
        """Add sender to blocklist in gateway.yaml."""
        self._update_list("blocklist", sender_email)

    def _update_list(self, list_name: str, sender_email: str) -> None:
        """Add sender to list in gateway.yaml (idempotent)."""
        if not self.config_path.exists():
            return

        config = yaml.safe_load(self.config_path.read_text()) or {}
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

            self.config_path.write_text(yaml.dump(config, default_flow_style=False))
            self._reload()
