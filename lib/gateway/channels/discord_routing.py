"""Discord inbound message routing + interaction-id helpers.

Pure functions, no I/O — the Discord twin of ``telegram_routing``. The
channel layer (``discord.py``) supplies the booleans these read; keeping the
decision pure makes the security-critical gating unit-testable without a live
``discord.py`` event loop.
"""

from __future__ import annotations


def should_process_message(
    *,
    is_dm: bool,
    mentioned: bool,
    replied_to_bot: bool,
    channel_allowlisted: bool,
) -> bool:
    """Decide whether a guild/DM message should reach the brain.

    Mirrors ``telegram_routing.should_process_message`` semantics:

      - DM → always answer.
      - Guild channel → answer only if the bot is **@mentioned**, the message
        **replies to the bot's** message, or the channel is explicitly
        allowlisted as "always answer". Otherwise stay silent.

    This is the "tag the bot in the group to answer" rule. It is a gate on top
    of authorization (``_is_authorized``): an authorized-but-not-mentioned
    guild message is dropped silently, exactly like Telegram groups.
    """
    if is_dm:
        return True
    return bool(mentioned or replied_to_bot or channel_allowlisted)


# ---------------------------------------------------------------------------
# Interaction (button) custom_id contract
# ---------------------------------------------------------------------------
#
# Discord message components carry a ``custom_id`` string (<=100 bytes). We
# reuse the exact token contract the Telegram inline keyboard uses
# (``cards.build_action_keyboard``): ``act:<verb>:<short_token>`` where
# ``verb`` is ``stop`` or ``bg`` and ``short_token`` is the 12-char action
# session token. One parser, both channels.

_ACTION_PREFIX = "act:"
_ACTION_VERBS = ("stop", "bg")


def parse_action_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Parse ``act:<verb>:<short_token>`` → ``(verb, short_token)`` or None.

    Returns None for anything that is not a well-formed supervisor-action
    component id, so the interaction handler can ignore unrelated components
    (e.g. the dead ``act:bg:done`` placeholder leaves ``short_token='done'``,
    which the registry resolves to "session already ended").
    """
    if not custom_id or not custom_id.startswith(_ACTION_PREFIX):
        return None
    try:
        _, verb, short_token = custom_id.split(":", 2)
    except ValueError:
        return None
    if verb not in _ACTION_VERBS or not short_token:
        return None
    return verb, short_token
