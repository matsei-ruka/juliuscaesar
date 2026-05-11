"""Exit codes + typed error class for deep-research.

The CLI surfaces these codes verbatim to operators / agents so they can
script around specific failures (e.g. exit 10 → trigger a `jc research
login`). Numbers and messages are part of the public contract — do not
renumber without bumping the SKILL.md.
"""

from __future__ import annotations


EXIT_OK = 0
EXIT_AUTH_REQUIRED = 10
EXIT_CAPTCHA = 11
EXIT_DEEP_RESEARCH_UNAVAILABLE = 12
EXIT_QUOTA = 13
EXIT_SELECTORS_FAILED = 14
EXIT_BROWSER_CRASH = 15
EXIT_BUSY = 16
EXIT_INVALID_INPUT = 17


CODE_LABELS: dict[int, str] = {
    EXIT_OK: "ok",
    EXIT_AUTH_REQUIRED: "auth_required",
    EXIT_CAPTCHA: "captcha",
    EXIT_DEEP_RESEARCH_UNAVAILABLE: "deep_research_unavailable",
    EXIT_QUOTA: "quota",
    EXIT_SELECTORS_FAILED: "selectors_failed",
    EXIT_BROWSER_CRASH: "browser_crash",
    EXIT_BUSY: "busy",
    EXIT_INVALID_INPUT: "invalid_input",
}


class DeepResearchError(RuntimeError):
    """Carries an exit code so callers can map it into CLI status."""

    def __init__(self, code: int, message: str, *, last_url: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.last_url = last_url

    @property
    def label(self) -> str:
        return CODE_LABELS.get(self.code, f"code_{self.code}")
