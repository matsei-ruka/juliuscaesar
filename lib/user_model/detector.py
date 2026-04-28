"""Pattern detection over conversation corpus."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .corpus import Event, iter_events
from .conf import UserModelConfig

# Stopwords (conservative set for English)
_STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are aren't as at be
    because been before being below between both but by can't did didn't do
    does doesn't doing don't down during each few for from further had hadn't
    has hasn't have haven't having he he'd he'll he's her here here's hers herself
    him himself his how how's i i'd i'll i'm i've if in into is isn't it it's
    its itself let's me more most mustn't my myself no nor not of off on once only
    or other ought our ours ourselves out over own same shan't she she'd she'll
    she's should shouldn't so some such than that that's the their theirs them
    themselves then there there's these they they'd they'll they're they've this
    those through to too under until up very was wasn't we we'd we'll we're we've
    were weren't what what's when when's where where's which while who who's whom
    why why's with won't would wouldn't you you'd you'll you're you've your
    yours yourself yourselves
    """.split()
)


@dataclass(frozen=True)
class Signal:
    """A single observation that may become a proposal."""
    kind: str  # recurring_topic | comm_pref | priority_shift | new_entity | rule_drift
    term: str | None = None  # for topic/entity signals
    count: int | None = None
    prev_count: int | None = None  # for priority_shift
    curr_count: int | None = None
    delta: int | None = None
    sample_event_ids: list[int] | None = None
    dimension: str | None = None  # for comm_pref
    current_value: float | None = None
    observed_value: float | None = None
    rule_excerpt: str | None = None
    severity: str = "medium"  # low | medium | high


def detect_all(
    instance_dir: Path,
    events: list[Event],
    config: UserModelConfig,
    current_user_md: str,
) -> Iterator[Signal]:
    """Run all enabled detectors."""
    if config.detectors.recurring_topic:
        yield from _detect_recurring_topics(events, config, current_user_md)
    if config.detectors.comm_pref:
        yield from _detect_comm_pref(events, config)
    if config.detectors.priority_shift:
        yield from _detect_priority_shift(events, config, current_user_md)
    if config.detectors.new_entity:
        yield from _detect_new_entities(events, config, current_user_md)
    if config.detectors.rule_drift:
        yield from _detect_rule_drift(events, config, current_user_md)


def _tokenize(text: str) -> list[str]:
    """Tokenize and dedupe against stopwords."""
    tokens = re.findall(r"\b[a-z]{3,}\b", text.lower())
    return [t for t in tokens if t not in _STOPWORDS]


def _detect_recurring_topics(
    events: list[Event],
    config: UserModelConfig,
    current_user_md: str,
) -> Iterator[Signal]:
    """Find terms appearing >= min_evidence_count times across >= 2 conversations."""
    all_tokens = Counter()
    term_to_events = defaultdict(set)
    term_to_convs = defaultdict(set)

    for evt in events:
        tokens = _tokenize(evt.content)
        for token in tokens:
            all_tokens[token] += 1
            term_to_events[token].add(evt.id)
            term_to_convs[token].add(evt.conversation_id)

    current_tokens = set(_tokenize(current_user_md))

    for term, count in all_tokens.most_common():
        if count < config.min_evidence_count:
            break
        if term in current_tokens:
            continue
        if len(term_to_convs[term]) < 2:
            continue
        yield Signal(
            kind="recurring_topic",
            term=term,
            count=count,
            sample_event_ids=sorted(list(term_to_events[term]))[:3],
        )


def _detect_comm_pref(events: list[Event], config: UserModelConfig) -> Iterator[Signal]:
    """Infer communication preferences from message characteristics."""
    if not events:
        return

    lengths = [len(evt.content) for evt in events]
    avg_length = sum(lengths) / len(lengths) if lengths else 0

    # Signal if there's a strong trend toward short or long messages.
    if avg_length < 100:
        yield Signal(
            kind="comm_pref",
            dimension="message_length",
            current_value=avg_length,
            observed_value=100,
        )
    elif avg_length > 500:
        yield Signal(
            kind="comm_pref",
            dimension="message_length",
            current_value=avg_length,
            observed_value=500,
        )


def _detect_priority_shift(
    events: list[Event],
    config: UserModelConfig,
    current_user_md: str,
) -> Iterator[Signal]:
    """Detect shift in entity (person, business) frequency."""
    # Simple heuristic: look for capitalized n-grams (entities) in content.
    entity_pattern = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b")
    entity_freq = Counter()

    for evt in events:
        entities = entity_pattern.findall(evt.content)
        for ent in entities:
            if len(ent) > 2:  # Skip single letters, abbreviations
                entity_freq[ent] += 1

    existing_entities = set(re.findall(r"\[\[([^\]|]+)", current_user_md))

    for entity, count in entity_freq.most_common():
        if entity in existing_entities:
            continue
        # For v1, we only surface high-frequency entities.
        if count >= config.min_evidence_count * 2:
            yield Signal(
                kind="priority_shift",
                term=entity,
                curr_count=count,
                prev_count=0,
                delta=count,
            )


def _detect_new_entities(
    events: list[Event],
    config: UserModelConfig,
    current_user_md: str,
) -> Iterator[Signal]:
    """Detect named entities not yet in memory."""
    entity_pattern = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b")
    entity_freq = Counter()

    for evt in events:
        entities = entity_pattern.findall(evt.content)
        for ent in entities:
            if len(ent) > 2:
                entity_freq[ent] += 1

    # Extract entities from wikilinks (both sides of [[name|display]])
    existing_entities = set()
    for match in re.finditer(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", current_user_md):
        existing_entities.add(match.group(1))  # The link target
        if match.group(2):
            existing_entities.add(match.group(2))  # The display text

    for entity, count in entity_freq.most_common():
        if entity in existing_entities:
            continue
        if count >= config.min_evidence_count:
            yield Signal(
                kind="new_entity",
                term=entity,
                count=count,
                sample_event_ids=[],
            )


def _detect_rule_drift(
    events: list[Event],
    config: UserModelConfig,
    current_user_md: str,
) -> Iterator[Signal]:
    """Detect contradictions between current rules and recent behavior."""
    # For v1, we look for simple signals in the RULES section.
    rules_section = re.search(r"## Standing rules(.*?)(?=##|$)", current_user_md, re.DOTALL)
    if not rules_section:
        return

    rules_text = rules_section.group(1)

    # Extract rule keywords (very conservative).
    keywords = [
        ("MarkdownV2", "plain text"),  # example rule: Telegram uses MarkdownV2
        ("07:00", "different time"),  # example: proactive ideas only at 07:00
    ]

    for rule_keyword, contradiction in keywords:
        if rule_keyword not in rules_text:
            continue
        # Simple heuristic: look for the opposite in recent events.
        for evt in events:
            if rule_keyword.lower() not in evt.content.lower():
                continue
            # Found a mention — check if it contradicts the rule.
            if rule_keyword == "MarkdownV2" and "**" in evt.content:
                yield Signal(
                    kind="rule_drift",
                    rule_excerpt=f"Telegram = {rule_keyword}",
                    severity="medium",
                    sample_event_ids=[evt.id],
                )
