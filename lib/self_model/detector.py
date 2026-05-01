"""Pattern detection over agent self-observation corpus."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .conf import SelfModelConfig
from .corpus import Event, iter_assistant_messages, iter_hot_observations, iter_user_messages


# Keyword lists — cabled but unused while detectors are disabled.
FILIPPO_CORRECTION_KEYWORDS = [
    "hai sbagliato",
    "non è giusto",
    "correggimi",
    "ricontrolla",
    "non quadra",
    "rivedi",
]

DIRECT_REQUEST_KEYWORDS = [
    "rifletti",
    "auto-osserva",
    "review yourself",
    "self-check",
    "guarda il tuo pattern",
]

EPISODE_FLAG_KEYWORDS = [
    "ho ceduto",
    "ho sbagliato",
    "errore mio",
    "ho perso il filo",
    "ho mancato",
    "non l'ho visto",
    "scivolato",
    "drift mio",
]


@dataclass(frozen=True)
class Signal:
    """A single self-observation that may become a proposal."""
    kind: str  # filippo_correction | hot_flag | direct_request | episode_flag | scan_weekly
    trigger: str
    text_excerpt: str
    conversation_id: str | None = None
    ts: str | None = None
    severity: str = "medium"  # low | medium | high
    evidence: list[str] | None = None


def detect_all(
    instance_dir: Path,
    events: list[Event],
    config: SelfModelConfig,
    current_rules_md: str,
) -> Iterator[Signal]:
    """Run all enabled detectors. All disabled by default in scaffold mode."""
    if config.detectors.filippo_correction:
        yield from detect_filippo_correction(events, config)
    if config.detectors.hot_flag:
        yield from detect_hot_flag(instance_dir, config)
    if config.detectors.direct_request:
        yield from detect_direct_request(events, config)
    if config.detectors.episode_flag:
        yield from detect_episode_flag(events, config)
    if config.detectors.scan_weekly:
        yield from detect_scan_weekly(instance_dir, events, config)


def detect_filippo_correction(events: list[Event], config: SelfModelConfig) -> Iterator[Signal]:
    """Detect Filippo's chat messages that contain correction keywords.

    TODO: events list passed to detect_all only contains assistant messages. Filippo
    correction lives in user-side messages. To activate, the runner must additionally
    pass user-message events from `corpus.iter_user_messages()`. For now, this returns
    [] even when the flag is on.
    """
    if not config.detectors.filippo_correction:
        return
    for evt in events:
        if evt.role != "user":
            continue
        text_lower = evt.text.lower()
        for kw in FILIPPO_CORRECTION_KEYWORDS:
            if kw in text_lower:
                yield Signal(
                    kind="filippo_correction",
                    trigger=kw,
                    text_excerpt=evt.text[:280],
                    conversation_id=evt.conversation_id,
                    ts=evt.ts,
                    severity="high",
                    evidence=[evt.id],
                )
                break


def detect_hot_flag(instance_dir: Path, config: SelfModelConfig) -> Iterator[Signal]:
    """Detect H2 blocks in HOT.md tagged `#self-observation`."""
    if not config.detectors.hot_flag:
        return
    for obs in iter_hot_observations(instance_dir):
        yield Signal(
            kind="hot_flag",
            trigger="#self-observation",
            text_excerpt=(obs.heading + "\n" + obs.body)[:280],
            conversation_id=None,
            ts=None,
            severity="medium",
            evidence=[obs.heading],
        )


def detect_direct_request(events: list[Event], config: SelfModelConfig) -> Iterator[Signal]:
    """Detect explicit auto-review requests in Filippo's chat messages."""
    if not config.detectors.direct_request:
        return
    for evt in events:
        if evt.role != "user":
            continue
        text_lower = evt.text.lower()
        for kw in DIRECT_REQUEST_KEYWORDS:
            if kw in text_lower:
                yield Signal(
                    kind="direct_request",
                    trigger=kw,
                    text_excerpt=evt.text[:280],
                    conversation_id=evt.conversation_id,
                    ts=evt.ts,
                    severity="high",
                    evidence=[evt.id],
                )
                break


def detect_episode_flag(events: list[Event], config: SelfModelConfig) -> Iterator[Signal]:
    """Detect agent's OWN outputs containing self-recognition keywords."""
    if not config.detectors.episode_flag:
        return
    for evt in events:
        if evt.role != "assistant":
            continue
        text_lower = evt.text.lower()
        for kw in EPISODE_FLAG_KEYWORDS:
            if kw in text_lower:
                yield Signal(
                    kind="episode_flag",
                    trigger=kw,
                    text_excerpt=evt.text[:280],
                    conversation_id=evt.conversation_id,
                    ts=evt.ts,
                    severity="medium",
                    evidence=[evt.id],
                )
                break


def detect_scan_weekly(
    instance_dir: Path,
    events: list[Event],
    config: SelfModelConfig,
) -> Iterator[Signal]:
    """Weekly sweep for character_emergence + error_pattern.

    Placeholder — returns []. Full implementation will aggregate JOURNAL.md entries
    from the past 7 days, look for repeated `Ipotesi pattern` matches across entries,
    and surface character-level signals for the proposer.
    """
    if not config.detectors.scan_weekly:
        return
    return
    yield  # pragma: no cover (generator marker)
