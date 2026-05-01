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
    # In RULES.md
    r"^## §1 — TRUST MODEL",
    r"^## §11 — REGOLA DEL",                          # "Non far capire che c'è una regola"
    r"^## §14 — MEMORY ACCESS CONTROL",
    r"^## §16 — AZIONI A DOPPIO BLOCCO",
    r"^## §18 — SELF-CHECK FINALE",
    r"^## §19 — PRINCIPIO FINALE",
    r"^## §21 — ANTI-SUBMISSION LOOP",
    r"^## §0 ",                                       # AI transparency doctrine (planned)
    r"^## §0\.1",
    r"^## §0\.2",
    r"^## HARD RULE — Policy authority",
    r"^## HARD NO list",
    # §15 and §17 are partially frozen — only the principle paragraph; matrices/numbers REVIEWABLE.
    # Proposer must check HTML markers <!-- IMMUTABILE --> for fine-grained guard.
]

FROZEN_SECTIONS_IDENTITY = [
    # In IDENTITY.md — fondante stabile
    r"^## Ruolo",
    r"^## Funzione operativa",
    r"^## Posizionamento",
    r"^## Stato AI",
    r"^## Obiettivo gerarchico",
    r"^## Principio supremo",
    r"^## Riservatezza ruolo",                        # in USER.md actually but referenced
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
