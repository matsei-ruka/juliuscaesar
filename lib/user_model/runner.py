"""Run cycle: detect + propose + notify."""

from __future__ import annotations

import logging
from pathlib import Path

from .conf import load_config
from .corpus import iter_events
from .detector import detect_all
from .proposer import generate_proposals
from .store import save_proposal, count_proposals


def run_now(instance_dir: Path) -> int:
    """Execute one detection + proposal cycle."""
    config = load_config(instance_dir)
    if not config.enabled:
        return 0

    logger = _setup_logging(instance_dir)
    logger.info("user_model cycle start")

    try:
        # 1. Load current user model
        user_md_path = instance_dir / "memory" / "L1" / "USER.md"
        if not user_md_path.exists():
            logger.warning("USER.md not found")
            return 1
        current_user_md = user_md_path.read_text(encoding="utf-8")

        # 2. Read events from queue
        events = list(iter_events(instance_dir, config.look_back_days))
        if not events:
            logger.info("no events in window")
            return 0
        logger.info(f"read {len(events)} events")

        # 3. Detect signals
        signals = list(detect_all(instance_dir, events, config, current_user_md))
        if not signals:
            logger.info("no signals detected")
            return 0
        logger.info(f"detected {len(signals)} signals")

        # 4. Generate proposals
        proposals = list(generate_proposals(instance_dir, signals, config, current_user_md))
        if not proposals:
            logger.info("no proposals generated")
            return 0
        logger.info(f"generated {len(proposals)} proposals")

        # 5. Save proposals
        for proposal in proposals:
            save_proposal(instance_dir, proposal, "staging")
            logger.info(f"saved proposal {proposal.id}")

        # 6. Notify (if configured)
        if config.notify_chat_id:
            _notify_telegram(instance_dir, proposals, config)

        logger.info("user_model cycle complete")
        return 0

    except Exception as e:
        logger.error(f"cycle failed: {e}", exc_info=True)
        return 1


def _setup_logging(instance_dir: Path) -> logging.Logger:
    """Setup logging to heartbeat/state/user_model.log."""
    log_dir = instance_dir / "heartbeat" / "state"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "user_model.log"

    logger = logging.getLogger("user_model")
    logger.setLevel(logging.INFO)

    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)

    return logger


def _notify_telegram(instance_dir: Path, proposals, config) -> None:
    """Send Telegram summary of proposals."""
    try:
        import subprocess
        from lib.heartbeat.lib import send_telegram as send_telegram_module
    except ImportError:
        return

    if not proposals:
        return

    body = f"🤖 *User model proposals ({len(proposals)})*\n\n"
    for i, p in enumerate(proposals[:5], 1):
        body += f"{i}. `{p.id}` — {p.type} {p.target_section or 'TOP'}\n"
        body += f"   Confidence: {p.confidence:.0%} | {p.reasoning[:50]}...\n"

    try:
        from lib.gateway.queue import connect
        # Use send_telegram script
        proc = subprocess.run(
            ["bash", "-c", f"echo '{body}' | {__file__}"],
            timeout=10,
            capture_output=True,
        )
    except Exception:
        pass
