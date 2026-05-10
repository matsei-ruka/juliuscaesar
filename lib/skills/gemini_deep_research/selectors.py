"""Gemini web UI selectors + version pin.

Best-effort fast paths. The runner tries each `Selector` first; on miss it
hands the same step to a browser-use agent. When Google ships a redesign,
bump `UI_VERSION` and update the strings — the agent fallback keeps the
skill working until a human patches selectors.
"""

from __future__ import annotations

from dataclasses import dataclass


UI_VERSION = "2026-05"


@dataclass(frozen=True)
class Selector:
    """One step's deterministic Playwright hint(s).

    `primary` is the preferred CSS / role selector; `alternates` are tried
    in order before falling back to the browser-use agent.
    """

    name: str
    primary: str
    alternates: tuple[str, ...] = ()
    goal: str = ""

    def all(self) -> tuple[str, ...]:
        return (self.primary, *self.alternates)


CHAT_INPUT = Selector(
    name="chat_input",
    primary='div[contenteditable="true"][role="textbox"]',
    alternates=(
        'textarea[aria-label*="Enter a prompt"]',
        '[data-test-id="chat-input"]',
    ),
    goal="Locate the Gemini prompt input and focus it.",
)

MODEL_SWITCH = Selector(
    name="model_switch",
    primary='button[aria-label*="model" i]',
    alternates=(
        'button[aria-label*="Switch" i]',
        '[data-test-id="model-selector"]',
    ),
    goal="Open the Gemini model selector and pick Deep Research.",
)

DEEP_RESEARCH_OPTION = Selector(
    name="deep_research_option",
    primary='[role="menuitem"]:has-text("Deep Research")',
    alternates=(
        'button:has-text("Deep Research")',
        '[data-test-id*="deep-research" i]',
    ),
    goal="Click the Deep Research entry in the model menu.",
)

PLAN_CARD = Selector(
    name="plan_card",
    primary='button:has-text("Start research")',
    alternates=(
        'button:has-text("Begin research")',
        '[data-test-id*="start-research" i]',
    ),
    goal="Confirm and start the research plan that Gemini proposes.",
)

EXPORT_MD = Selector(
    name="export_markdown",
    primary='button:has-text("Export"):not([disabled])',
    alternates=(
        '[aria-label*="Export" i]',
        'button:has-text("Copy")',
    ),
    goal="Open the export menu and pick Markdown / Copy.",
)

SOURCES_LIST = Selector(
    name="sources_list",
    primary='[aria-label*="Sources" i]',
    alternates=(
        'div:has-text("Sources") + ul',
        '[data-test-id*="sources" i]',
    ),
    goal="Read the sources sidebar / panel.",
)


SIGNED_OUT_URL_FRAGMENTS = ("accounts.google.com/", "/signin", "/ServiceLogin")
CAPTCHA_FRAGMENTS = ("captcha", "unusual traffic", "verify you")
QUOTA_FRAGMENTS = ("quota", "limit reached", "try again later")
COMPLETION_TEXT_FRAGMENTS = ("Research complete", "Export", "Copy report")


__all__ = [
    "UI_VERSION",
    "Selector",
    "CHAT_INPUT",
    "MODEL_SWITCH",
    "DEEP_RESEARCH_OPTION",
    "PLAN_CARD",
    "EXPORT_MD",
    "SOURCES_LIST",
    "SIGNED_OUT_URL_FRAGMENTS",
    "CAPTCHA_FRAGMENTS",
    "QUOTA_FRAGMENTS",
    "COMPLETION_TEXT_FRAGMENTS",
]
