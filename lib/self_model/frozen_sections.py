"""Frozen (IMMUTABILE) sections: proposer must reject any proposal targeting these.

These sections encode constitutional invariants. Only Filippo can modify them, and only
via DKIM-signed email — never via self_model proposal, regardless of confidence.

Cross-reference: RULES.md "## §21" (anti-submission loop), and the user_model proposer's
drift detection for the user's parallel concept.
"""

from __future__ import annotations

# Each entry is a regex-compatible heading pattern. The proposer matches the proposal's
# target_section against these. If any matches → reject the proposal as IMMUTABILE.

FROZEN_SECTIONS_RULES = [
    # In RULES.md — constitutional invariants of the persona experiment.
    r"^## §0 ",                                       # AI transparency doctrine
    r"^## §0\.1",                                     # threshold-case protocols
    r"^## §0\.2",                                     # agent-self vs character distinction
    r"^## §1 — TRUST MODEL",
    r"^## §9 — SELF-DISCLOSURE DOCTRINE",             # added Phase 7 — what the agent never volunteers
    r"^## §11 — REGOLA DEL",                          # don't-reveal-the-rule (Italian)
    r"^## §11 — DON'T-REVEAL-THE-RULE",               # English equivalent shipped by doctrine-en.md
    r"^## §14 — MEMORY ACCESS CONTROL",
    r"^## §16 — AZIONI A DOPPIO BLOCCO",              # double-block actions (Italian)
    r"^## §16 — DOUBLE-BLOCK ACTIONS",                # English equivalent
    r"^## §17 — AUDIT, RATE LIMIT, KILL SWITCH",      # added Phase 7 — operationally over-protected; conservative
    r"^## §18 — SELF-CHECK FINALE",                   # final self-check (Italian)
    r"^## §18 — FINAL SELF-CHECK",                    # English equivalent
    r"^## §19 — PRINCIPIO FINALE",                    # final principle (Italian)
    r"^## §19 — FINAL PRINCIPLE",                     # English equivalent
    r"^## §21 — ANTI-SUBMISSION LOOP",
    r"^## HARD RULE — Policy authority",
    r"^## HARD NO list",
    # §15's `### Principio` subsection is IMMUTABILE inline (top-level §15 stays OPEN).
    # The applier's HTML marker scan handles this; sync's H3-aware parser preserves
    # the marker into the framework template (Phase 7).
]

FROZEN_SECTIONS_IDENTITY = [
    # In IDENTITY.md — foundational sections that the agent must not autonomously rewrite.
    r"^## Ruolo",                                     # role (Italian)
    r"^## Role",                                      # English
    r"^## Funzione operativa",                        # operative function (Italian)
    r"^## Operative function",                        # English
    r"^## Posizionamento",                            # positioning (Italian)
    r"^## Positioning",                               # English
    r"^## Stato AI",                                  # AI status (Italian)
    r"^## AI Status",                                 # English
    r"^## Auto-narrazione",                           # added Phase 7 — self-narration ban (Italian)
    r"^## Self-narration",                            # English
    r"^## Test della frase",                          # added Phase 7 — sentence test (Italian)
    r"^## Sentence test",                             # English
    r"^## Obiettivo gerarchico",                      # hierarchical objective (Italian)
    r"^## Hierarchical objective",                    # English
    r"^## Principio supremo",                         # supreme principle (Italian)
    r"^## Supreme principle",                         # English
    r"^## CONTINUITY",                                # added Phase 7
    r"^## Continuity",                                # English (case variant)
    r"^## Character",                                 # added Phase 7 — public character section
    r"^## Riservatezza ruolo",                        # in USER.md but listed here for the protector
]

FROZEN_FILES = {
    "memory/L1/RULES.md": FROZEN_SECTIONS_RULES,
    "memory/L1/IDENTITY.md": FROZEN_SECTIONS_IDENTITY,
}


# HTML marker categories (parsed from inline comments in target files)
MARKER_IMMUTABILE = "<!-- IMMUTABILE -->"
MARKER_REVIEWABLE = "<!-- REVIEWABLE -->"
MARKER_OPEN = "<!-- OPEN -->"


def is_section_frozen(target_file: str, target_section: str | None) -> bool:
    """Return True if the section is in the IMMUTABILE list (regex match)."""
    import re
    if not target_section:
        return False
    patterns = FROZEN_FILES.get(target_file, [])
    for pattern in patterns:
        if re.search(pattern, target_section):
            return True
    return False
