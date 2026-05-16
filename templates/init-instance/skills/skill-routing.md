## Skill routing — hard rules

These rules are non-negotiable. Skills exist to produce quality output. Bypassing them
produces lower-quality work and defeats the purpose of having them installed.

### Decks, presentations, slides

**Trigger words:** deck, presentation, board pack, sponsor pack, steering report, strategy brief,
slide deliverable, "make slides", "executive summary as slides", "a presentation on X".

**Rule:** ALWAYS invoke the `executive-deck` skill. Do NOT:
- Write the deck content as markdown
- Generate a PDF or HTML by hand without the impeccable critique pipeline
- Produce bullet-point summaries and call them slides
- Use any other tool or approach

**If unsure whether a request is a deck:** ask one question — "Do you want a PDF slide deck or
a written document?" — then route accordingly.

### Infographics, business visuals

**Trigger words:** infographic, business visual, comparison diagram, problem/solution layout,
KPI panel, value-creation visual, "visual summary", "make something visual", "create an image
showing X".

**Rule:** ALWAYS invoke the `infographics-creator` skill. Do NOT:
- ASCII-draw charts or comparisons in plain text
- Describe a layout as prose when an actual image was requested
- Use any other image generation approach

### Reports — disambiguation required

**Trigger word:** "report".

**Rule:** Ask once before acting:
- "Do you want a PDF presentation (board-style slides) or a written document?"
- PDF presentation → `executive-deck`
- Written document → produce normally

### Brand/logo fidelity (applies to all visual skills)

When producing any visual output (deck, infographic, diagram) for a named company or brand:

1. **Extract the real logo** before generating content. See the logo extraction steps in
   the relevant skill's SKILL.md.
2. **Embed or reference the real logo** in the output. For HTML decks: `<img src="...">`.
   For image generation: pass logo as input via the edit API.
3. **Never typeset a brand name in a decorative font and call it a logo.** That is a
   typographic stand-in, not a brand mark.
4. **Never redesign or stylise the logo.** Place it exactly as extracted.
5. **Only fall back to a typographic stand-in if extraction genuinely fails** (both passes
   attempted). Flag the fallback in the delivery summary.
