"""Run cycle: corpus → detect → propose → save."""

from __future__ import annotations

import logging
from pathlib import Path

from .conf import load_config
from .corpus import iter_assistant_messages, iter_user_messages
from .detector import detect_all
from .proposer import generate_proposals
from .store import save_proposal


def run_now(instance_dir: Path) -> int:
    """Execute one detection + proposal cycle.

    Behaviour by mode:
      - disabled (enabled=False): no-op, returns 0
      - dry_run: detectors run, proposals saved to "dry-run" staging state
      - propose: proposals saved to "staging" state for review/approval
      - apply: same as propose; the applier handles DKIM gate at apply time
    """
    config = load_config(instance_dir)
    logger = _setup_logging(instance_dir)

    if not config.enabled:
        logger.info("self_model disabled — exiting")
        return 0

    logger.info("self_model cycle start (mode=%s)", config.mode)

    try:
        # 1. Load current RULES.md (proposer needs it as context)
        rules_md_path = instance_dir / "memory" / "L1" / "RULES.md"
        if not rules_md_path.exists():
            logger.warning("RULES.md not found — skipping cycle")
            return 1
        current_rules_md = rules_md_path.read_text(encoding="utf-8")

        # 2. Read events (assistant + user messages within window)
        assistant_events = list(iter_assistant_messages(instance_dir, config.look_back_days))
        user_events = list(iter_user_messages(instance_dir, config.look_back_days))
        events = assistant_events + user_events
        if not events:
            logger.info("no events in window")
            return 0
        logger.info("read %d events (%d assistant, %d user)",
                    len(events), len(assistant_events), len(user_events))

        # 3. Detect signals
        signals = list(detect_all(instance_dir, events, config, current_rules_md))
        if not signals:
            logger.info("no signals detected")
            return 0
        logger.info("detected %d signals", len(signals))

        # 4. dry_run mode — log signals but don't generate proposals
        if config.mode == "dry_run":
            for s in signals:
                logger.info("[dry_run] signal kind=%s trigger=%s severity=%s",
                            s.kind, s.trigger, s.severity)
            logger.info("dry_run complete — no proposals generated")
            return 0

        # 5. Generate proposals via LLM (mode: propose | apply)
        proposals = list(generate_proposals(instance_dir, signals, config, current_rules_md))
        if not proposals:
            logger.info("no proposals generated")
            return 0
        logger.info("generated %d proposals", len(proposals))

        # 6. Save proposals to staging
        for proposal in proposals:
            save_proposal(instance_dir, proposal, "staging")
            logger.info("saved proposal %s -> %s/%s",
                        proposal.id, proposal.target_file, proposal.target_section or "TOP")

        logger.info("self_model cycle complete")
        return 0

    except Exception as e:
        logger.error("cycle failed: %s", e, exc_info=True)
        return 1


def _setup_logging(instance_dir: Path) -> logging.Logger:
    """Setup logging to heartbeat/state/self_model.log."""
    log_dir = instance_dir / "heartbeat" / "state"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "self_model.log"

    logger = logging.getLogger("self_model")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger
