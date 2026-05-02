# Agent-Driven Persona Fill

**Status:** Shipping
**Date:** 2026-05-02
**For:** Any JuliusCaesar agent tasked with improving a persona

## Goal

Fill missing/unfilled persona slots autonomously. Agent reads what's needed, knows what to ask the user, fills from context. User never sees schema — only natural questions.

## Prerequisites

- Instance has `jc-persona` CLI (ships with JuliusCaesar).
- Instance has a `templates/persona-interview/questions.yaml` defining slots.
- User (principal) available for clarifications.

## The Loop

```
┌─────────────────────────────────────────────┐
│ Agent: Run jc-persona gaps --json           │
└────────────────┬────────────────────────────┘
                 │ (tool output, internal only)
┌────────────────▼────────────────────────────┐
│ Agent: Parse gaps. For each gap:            │
│   - Can I fill from context/memory?         │
│   - If yes: fill directly, move on          │
│   - If no: ask user ONE targeted question   │
└────────────────┬────────────────────────────┘
                 │ (user sees natural language Q)
┌────────────────▼────────────────────────────┐
│ User: Answers (or corrects if agent        │
│       already filled something)              │
└────────────────┬────────────────────────────┘
                 │ (agent receives answer)
┌────────────────▼────────────────────────────┐
│ Agent: Edit target file, add/replace        │
│       slot section with answer               │
└────────────────┬────────────────────────────┘
                 │
┌────────────────▼────────────────────────────┐
│ Agent: Run jc-persona gaps --json again     │
│        Confirm gap count decreased           │
└────────────────┬────────────────────────────┘
                 │
          ┌──────▼──────┐
          │ Any gaps    │
          │ left?       │
          └──┬────────┬─┘
            yes      no
             │        │
             │        └──→ [Done. Commit + push.]
             │
             └──────────────────┐
                                │
                        [Loop back to top]
```

## Step-by-step

### 1. Scan gaps

```bash
cd /path/to/instance
jc-persona gaps --json > /tmp/gaps.json
```

Parse the JSON. Count gaps by state. Identify which slots have `target_file` paths with `<slug>` or `<key>` placeholders.

**If unbound placeholders exist:**
- Check `ops/persona-macros.json` for bindings.
- If `persona.slug` missing: ask user for slug (kebab-case, usually the persona's short name).
- Write to `ops/persona-macros.json`: `{"persona.slug": "value", ...}`.
- Re-run gaps scan.

### 2. Prioritize

Organize gaps by type:
- **Known from context** — sections you already know from session memory, prior L1/L2 content, or the user's profile. Fill these silently.
- **Inferrable** — sections you can reason about from character or context. Make an educated guess; ask user to confirm/correct.
- **Unknown** — sections only the user knows. Ask a natural question.

### 3. Fill slots you know

For each slot where you have confidence:

a. Read the target file (`slot.target_file`, now macro-resolved).
b. Find the section heading (`slot.target_section`).
c. Extract or compose the answer.
d. Use the Edit tool to replace the section body (or create section if missing).

Example:
```
## Role

Executive strategist and tactical executor. Co-think partner for [user]'s 
business and personal moves at founder level.
```

### 4. Ask for the rest

For slots you can't fill:

a. Extract the first prompt's `text` field from the JSON.
b. Ask the user ONE question. Natural language, no slot IDs. Example:

   **Bad:** "Please fill characterbible.sport"
   **Good:** "What sport(s) do you practice regularly, and how seriously? E.g. daily running, weekend cycling, competitive tennis?"

c. User answers.
d. Compose the answer (combine multiple prompts if needed via `composition.template` from the slot).
e. Write to the target section.
f. Re-scan to confirm it's no longer a gap.

### 5. Loop until zero

Re-run `jc-persona gaps --json`. When the output shows zero gaps, commit and push.

```bash
git add -A
git commit -m "memory(L2): complete persona interview — zero gaps"
git push
```

## JSON shape reference

```json
{
  "gaps": [
    {
      "slot_id": "identity.role",
      "state": "missing",     // or "unfilled" or "populated"
      "slot": {
        "slot_id": "identity.role",
        "target_file": "memory/L1/IDENTITY.md",
        "target_section": "## Role",
        "kind": "text",
        "prompts": [
          {
            "id": "role_title",
            "text": "Persona's role title (e.g. ...)",
            "kind": "text",
            "help": "optional help text",
            "validation": {
              "required": true,
              "min_chars": 10
            },
            "depends_on": null
          }
        ],
        "composition": {
          "template": "{{role_title}}",
          "when": null,
          "fallback": null
        }
      }
    }
  ],
  "summary": {
    "total": 0,
    "missing": 0,
    "unfilled": 0,
    "populated": 0
  }
}
```

Key fields:
- `slot.target_file` — absolute path (macro-resolved)
- `slot.target_section` — exact heading match in the file (e.g. "## Role")
- `slot.prompts[].text` — question to answer
- `slot.prompts[].depends_on` — visibility condition (e.g. show only if another prompt equals "yes")
- `slot.composition` — template to assemble multiple prompt answers into one section

## Gotchas

**Exact heading match.** If the file has `## Role` but the slot expects `## Operative Role`, the gap detector thinks it's missing. Check the JSON to see the exact expected heading.

**Macro binding.** `<slug>` and `<key>` placeholders in `target_file` are resolved via `ops/persona-macros.json`. No binding → path with literal `<slug>` → file doesn't exist → gap. Bind first.

**Populated vs unfilled.** A section is "populated" if it exists and has content beyond the placeholder comment. An empty section with just `<!-- ASK: ... -->` is "unfilled" — still needs an answer.

**Depends_on.** Some prompts only appear if another prompt's answer meets a condition. E.g., "What color is your car?" depends on "Do you own a car? == yes". The UI handles this; the agent should too — don't ask a prompt if its visibility predicate is false.

**Composition.** Some slots combine multiple prompt answers into one section via a template. E.g., a "vehicle" slot might have prompts for "make/model/year" and "attitude", composed into one narrative paragraph. Read `slot.composition.template` to understand the final shape before asking individual prompts.

## Example: Rachel Zane

**Starting state:** 27 gaps (5 unfilled L1 sections, 22 missing character-bible/<slug> / cv/<slug>).

**Agent workflow:**
1. Scan gaps.
2. Spot 22 gaps have `<slug>` in path. Ask user: "What's your slug (short kebab-case name)?" User: "rachel". Write `ops/persona-macros.json`.
3. Re-scan. 22 missing → now resolvable. 14 character-bible, 8 cv.
4. Check if files exist. `character-bible/rachel.md` exists, full. 8 cv slots now "missing" because `cv/rachel.md` doesn't exist.
5. 5 unfilled L1 slots: fill from session context (communication prefs, channels, standing rules, identity background).
6. Create `cv/rachel.md` with standard sections from agent knowledge.
7. Re-scan. Zero gaps.
8. Commit + push.

**Result:** 27 → 0 in one session.

## For framework devs

The `jc-persona gaps --json` output is intentionally verbose — it carries all the context an agent needs to fill autonomously. No round-trips to the questions.yaml file required.

If you're building a new slot, ensure:
- `slot.target_section` is exact (tests check via regex match in the file).
- `slot.prompts[].text` is a clear, standalone question.
- `slot.composition` (if present) clearly shows how prompts combine.
- Validation rules are realistic (don't require 1000+ chars for a one-liner).

The agent's job is translation and judgment; the tool's job is clarity.
