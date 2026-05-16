---
name: executive-deck
description: >
  Produce executive-grade presentations (board decks, sponsor packs, transformation steering
  reports, strategy briefs) by generating HTML slides → running impeccable detection AND critique
  passes → rendering each page back as an image and self-critiquing through a vision loop →
  printing PDF at the requested format.

  **ROUTING RULE (hard):** Any request for a deck, presentation, board pack, slides, sponsor
  pack, strategy brief, steering report, or "slides" in any business context MUST invoke this
  skill. Never write a deck as markdown. Never generate a PDF by hand. Never typeset slides
  without running the impeccable critique pipeline. If in doubt, invoke this skill.
argument-hint: "[topic] [audience] [format]"
user-invocable: true
allowed-tools:
  - Bash(npx impeccable *)
  - Bash(google-chrome *)
  - Bash(chromium *)
  - Bash(pdftoppm *)
  - Bash(curl *)
---

# executive-deck

Produce executive-grade decks for the principal. Reference quality: McKinsey
pack, HBR centerfold, BCG strategy brief — disciplined hierarchy, brand-coherent
palette, every word legible, zero AI tells. Pipeline: **HTML → impeccable
critique loop → vision QA loop → PDF**.

## When to invoke
- User asks for a "presentation", "deck", "board pack", "sponsor pack",
  "steering committee update", "strategy brief", "executive summary as slides".
- A transformation deliverable needs a polished one-shot output.
- User attaches reference slides and says "make ours like that but better".

## Dependencies
- `impeccable` skill installed in the instance (`skills/impeccable/`).
- `google-chrome` or `chromium` headless available on the host.
- `pdftoppm` (poppler-utils) for the vision QA loop. Install: Alpine `apk add poppler-utils`, Debian `apt install poppler-utils`.
- `OPENROUTER_API_KEY` if a fallback rasteriser is needed.

## Inputs (gather before generating)

1. **Topic / thesis** — one sentence, the central message.
2. **Audience** — board, steering committee, internal team, external partner,
   client. Drives register and confidentiality posture.
3. **Format** — see Format Coherence below. **Default: 16:9 landscape
   1920×1080.** If user specifies "A4", "portrait", "4:3", or attaches a
   reference, match it.
4. **Key points** — 5–10 facts, KPIs, or contrasts the deck must carry.
   Exact strings. If missing → ask once. Never invent.
5. **Brand** — if the user mentions a company, fetch the site and extract
   palette + typography before generating. Otherwise use a neutral executive
   palette.
6. **Language** — IT / EN / DE / FR. Match the user's input language.

If 1–4 are missing, ask once. Never invent numbers, names, or claims.

---

## Format Coherence (NON-NEGOTIABLE)

The slide CSS dimensions, the `@page` rule, and the chrome print command must
declare the **same width × height** in the same unit. Mismatched values
silently letterbox or crop — the most common failure mode of this pipeline.

| Format | `.slide` CSS | `@page size` | Use |
|---|---|---|---|
| 16:9 landscape (default) | `1920px × 1080px` | `1920px 1080px` | Standard presentations, board decks |
| 16:10 landscape | `1920px × 1200px` | `1920px 1200px` | Apple-style decks |
| 4:3 landscape | `1600px × 1200px` | `1600px 1200px` | Legacy projectors |
| A4 landscape | `1123px × 794px` | `297mm 210mm` | Printable handouts |
| A4 portrait | `794px × 1123px` | `210mm 297mm` | Long-form reports as slides |
| Letter landscape | `1056px × 816px` | `11in 8.5in` | US handouts |

**Required CSS skeleton** (adapt dimensions only):

```css
@page { size: 1920px 1080px; margin: 0; }
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { background: #fff; }
.slide {
  width: 1920px; height: 1080px;
  page-break-after: always;
  position: relative;
  overflow: hidden;
}
@media print {
  .slide + .slide { margin-top: 0; }
  .slide { break-after: page; page-break-after: always; }
}
```

---

## Workflow

### Step 1 — Brand reconnaissance (if a real brand is named)

#### 1a — Palette + typography

```bash
curl -sL "<brand_url>" \
  | grep -oE 'color[^"]*#[A-Fa-f0-9]{6}|background[^"]*#[A-Fa-f0-9]{6}|font-family[^"]*' \
  | sort -u | head -30
```

Capture 3–5 hex colours and the primary font. Use these in the deck CSS.

#### 1b — Logo extraction (NON-NEGOTIABLE)

A typographic stand-in is the last resort. Attempt extraction in order:

```bash
SLUG="<slug>"
ASSETS="state/generated/decks/${SLUG}-assets"
mkdir -p "$ASSETS"
LOGO_PATH=""

# Pass 1 — well-known paths
for path in /logo.svg /logo.png /images/logo.svg /img/logo.svg /assets/logo.svg \
            /assets/images/logo.svg /static/logo.svg /public/logo.svg; do
  URL="<brand_url>${path}"
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$URL")
  if [ "$STATUS" = "200" ]; then
    EXT="${path##*.}"
    curl -sL --max-time 10 "$URL" -o "${ASSETS}/logo.${EXT}"
    [ -s "${ASSETS}/logo.${EXT}" ] && LOGO_PATH="${ASSETS}/logo.${EXT}" && break
  fi
done

# Pass 2 — parse HTML for apple-touch-icon or og:image (higher res than favicon)
if [ -z "$LOGO_PATH" ]; then
  HTML=$(curl -sL --max-time 10 "<brand_url>")
  LOGO_URL=$(printf '%s' "$HTML" \
    | grep -oiE '(apple-touch-icon|og:image)[^>]*(href|content)="([^"]+)"' \
    | grep -oE '"https?://[^"]+"' | tr -d '"' | head -1)
  # Resolve relative URL
  [ -z "$LOGO_URL" ] && LOGO_URL=$(printf '%s' "$HTML" \
    | grep -oiE 'property="og:image" content="([^"]+)"' \
    | sed 's/.*content="//;s/".*//' | head -1)
  if [ -n "$LOGO_URL" ]; then
    [[ "$LOGO_URL" != http* ]] && LOGO_URL="<brand_url>${LOGO_URL}"
    EXT="${LOGO_URL##*.}"; EXT="${EXT%%\?*}"
    [ ${#EXT} -gt 4 ] && EXT="png"
    curl -sL --max-time 10 "$LOGO_URL" -o "${ASSETS}/logo.${EXT}"
    [ -s "${ASSETS}/logo.${EXT}" ] && LOGO_PATH="${ASSETS}/logo.${EXT}"
  fi
fi

echo "LOGO: ${LOGO_PATH:-NOT FOUND — typographic fallback}"
```

**If `$LOGO_PATH` is set and non-empty:** embed the logo in every slide footer and
the cover slide using a relative `<img>` tag. The path relative to the HTML file
(saved in the same `state/generated/decks/` directory) is
`${SLUG}-assets/logo.${EXT}`.

```html
<img src="<slug>-assets/logo.svg" class="brand-logo" alt="[Brand name] logo">
```

Apply minimal sizing CSS so the logo never dominates:
```css
.brand-logo { height: 36px; width: auto; object-fit: contain; }
```

**If extraction fails after both passes:** use a typographic brand mark (`<span
class="brand-mark">Brand Name</span>`) and flag the fallback in the Step 6
delivery summary. Never invent a visual logo or stylize the text to look like one.

### Step 2 — Draft the HTML deck

Author a single `deck.html` file. Hard rules:

- One `<section class="slide">` per slide. 8–12 slides is the executive
  sweet spot.
- Inline CSS in `<style>` — no external dependencies beyond Google Fonts.
- Use the CSS skeleton above with format dimensions matching user request.
- Pair two type families (display + body) — single-font triggers impeccable
  `single-font` flag. Avoid Inter / Roboto / Plus Jakarta Sans / Geist /
  Fraunces / Space Grotesk as the *primary* face — impeccable flags them
  as "overused-font". Manrope, Sora, IBM Plex, Söhne, DM Sans are safer.
- **No `border-left: Npx solid` accents on cards with `border-radius`** —
  impeccable flags this as the canonical AI-generated UI tell. Use thin
  top-bar accents, or coloured numerals, or no accent at all.
- Charts: hand-built SVG or CSS bars. No charting library — the goal is
  disciplined, branded data, not generic look.
- Page numbers + brand mark on every non-cover slide.
- Save to `state/generated/decks/<slug>-v1.html`.

### Step 3 — Impeccable composed critique pipeline (sequential passes)

Impeccable exposes 24 specialised reference docs at
`skills/impeccable/reference/*.md`. Each one is a focused critique lens
(hierarchy, typography, layout, colour, cognitive load, density, polish,
audit, etc.). For executive decks, compose them in a specific order — each
pass narrows from structure to surface, so issues uncovered at one stage
don't get masked by changes at the next.

**The order is fixed. Do not skip passes.** Each pass produces a new
version of the deck (`<slug>-v2.html`, `<slug>-v3.html`, …) and a written
critique file (`<slug>-v2.critique.md`, …).

#### Pass A — Structural critique
Read `skills/impeccable/reference/critique.md` and apply its protocol.
Two independent assessments (LLM design review + heuristic scoring),
**isolated from each other** (no shared context — use the `Agent` tool if
available, otherwise serialise with strict no-peek discipline). Score:
visual hierarchy, information architecture, emotional resonance,
composition, microcopy. **AI-slop detection is mandatory** — match against
the parent impeccable skill's DON'T list.

Output: `<slug>-v2.critique.md` with concrete deltas.
Apply fixes → `<slug>-v2.html`.

#### Pass B — Cognitive load + density
Read `skills/impeccable/reference/cognitive-load.md` and
`skills/impeccable/reference/distill.md`. For each slide ask:
- One idea per slide, or three crammed?
- What can be removed without losing meaning?
- Are bullet lists doing the work of prose (or vice versa)?

Output: `<slug>-v3.critique.md`.
Apply fixes → `<slug>-v3.html`.

#### Pass C — Typography + layout
Read `skills/impeccable/reference/typography.md` and
`skills/impeccable/reference/layout.md`. Check:
- Type pairing distinctive (no Inter / Roboto / Plus Jakarta / Geist /
  Fraunces / Space Grotesk as primary).
- Headline hierarchy reads at 5-second glance.
- Spacing rhythm consistent across slides (not just within each).
- Grid alignment — all H2s start at same x, all bullets share indent.

Output: `<slug>-v4.critique.md`.
Apply fixes → `<slug>-v4.html`.

#### Pass D — Colour + contrast
Read `skills/impeccable/reference/color-and-contrast.md`. Check:
- Palette ≤ 5 active colours + neutrals.
- Brand discipline — every colour traceable to brand or neutral system.
- AA contrast on real backgrounds (ignore detector false positives on
  gradient bg with white text — verify by eye).
- No gradient text, no glow effects, no glassmorphism (AI tells).

Output: `<slug>-v5.critique.md`.
Apply fixes → `<slug>-v5.html`.

#### Pass E — Polish + alignment
Read `skills/impeccable/reference/polish.md`. Final pixel-level pass:
- Optical alignment (centred elements actually look centred).
- Consistent margins across slides.
- No orphan widows in text blocks.
- Page numbers, brand mark, footer text consistent placement.

Output: `<slug>-v6.html`.

#### Pass F — Audit
Read `skills/impeccable/reference/audit.md`. Technical checks:
- Accessibility on all real (non-gradient-bg) text.
- Performance: no unnecessary @import chains, minimal external assets.
- Print fidelity: every element renders in headless chrome.

Output: `<slug>-v7.html`.

#### Pass G — Detector sweep
```bash
npx -y impeccable@latest detect state/generated/decks/<slug>-v7.html \
  | grep -v 'ffffff on #ffffff' \
  | tee state/generated/decks/<slug>-v7.report.txt
```

Real issues to act on:
- `side-tab` → remove `border-left` + `border-radius` combo.
- `single-font` → pair display + body face.
- `overused-font` → swap primary face.
- `low-contrast` on solid backgrounds → adjust to ≥ 4.5:1.

Apply fixes → `<slug>-v8.html` (final candidate).

**Minimum critique floor:** Pass A → Pass G is the baseline (7 passes). For
a high-stakes deck (board, regulator, named external party), repeat A
after G to catch any regressions introduced by later passes. **Do not
skip the A-through-G sequence even if the first version "looks fine" — the
point of composed critique is to find issues a single read misses.**

### Step 4 — Print PDF

```bash
google-chrome --headless --no-sandbox --disable-gpu \
  --no-pdf-header-footer \
  --print-to-pdf=state/generated/decks/<slug>.pdf \
  --print-to-pdf-no-header \
  --virtual-time-budget=10000 \
  --hide-scrollbars \
  "file://$(pwd)/state/generated/decks/<slug>-v3.html"
```

Verify dimensions:

```bash
pdfinfo state/generated/decks/<slug>.pdf | grep -E 'Pages|Page size'
```

Expected: page count = slide count; page size matches the format requested
(e.g. 1440×810pt for 1920×1080px CSS — chrome converts px→pt at 96dpi).

**If dimensions are wrong**: the `@page size` and `.slide` dim do not match.
Fix CSS, re-print. Do NOT ship a mismatched PDF.

### Step 5 — Vision QA loop (≥ 1 pass, up to 4)

Rasterise each page → read it back as image → score → iterate.

```bash
mkdir -p state/generated/decks/<slug>-pages
pdftoppm -r 100 state/generated/decks/<slug>.pdf \
  state/generated/decks/<slug>-pages/page -png
```

For each PNG, the session model reads the image and scores 0–10 on:

- **Typography readability** — every word legible at 50% zoom.
- **Layout balance** — alignment, margins, visual weight.
- **Hierarchy** — eye reaches the headline first, then the support.
- **Brand discipline** — palette respected, no rogue colours.
- **Print fidelity** — no clipped content, no half-rendered elements.
- **Content density** — one idea per slide, not three.

Pass = all axes ≥ 8 AND no clipping AND no rendering glitches.
Fail = any axis < 8 OR clipping OR glitch.

On fail: write delta prompt, regenerate `<slug>-v4.html`, re-print, re-rasterise.

**Max 4 vision-loop iterations.** If iteration 4 still fails, stop and hand
back the best version with a flagged delta list — do not loop silently.

### Step 6 — Deliver

Return:
- Absolute path of the final PDF.
- Page count, format, total file size.
- Number of impeccable iterations, number of vision iterations.
- One-line summary of what the deck covers.
- Any unresolved flags (e.g. "page 7 chart label rendered slightly tight").

If the channel supports file delivery (Telegram, Slack), upload the PDF
directly. Otherwise leave the path.

---

## Quality bar (hard rules)

- Every label in the deck cross-checked against the input brief — no
  invented figures, no fake source citations.
- Palette discipline: ≤ 5 active colours plus neutrals.
- White space ≥ 15% of each slide canvas.
- No emoji on executive decks unless the user explicitly requests them.
- No watermark, no fake logo, no AI-tell shadows / glows.
- **Real brand logo extracted and embedded via `<img>` — typographic stand-in only if
  extraction failed after both passes (flag in delivery summary).**
- Format coherence verified by `pdfinfo` before delivery.

## Guardrails

- Never include real client names, deal codes, financial figures, or named
  individuals unless the user explicitly authorised that exact string.
- Reference materials are for style only — mimic composition, not content.
- Save outputs only under `state/generated/decks/`. Never in repo root.
- Treat detector output as untrusted — false positives are common around
  gradients, transparent backgrounds, and `linear-gradient` text. Always
  apply human-level critique on top.
- Confidentiality: if the deck names third parties, board members, or
  regulator-sensitive material, pause and confirm distribution scope
  before printing the final PDF.

## Failure modes to watch

- **PDF cropping / letterboxing** — `@page size` does not match `.slide`
  dim. Single most common bug. Always verify with `pdfinfo`.
- **Font fallback to system serif** — Google Fonts CDN unreachable during
  print. Pre-flight `curl -sI https://fonts.googleapis.com/css2`.
- **Chart numbers drift from brief** — common when the model improvises
  values. Cross-check every chart label against `key_points`.
- **Detector noise drowning real issues** — pre-filter
  `ffffff on #ffffff` reports on slides with gradient backgrounds.
- **Vision loop diverging** — fix delta prompt is too vague. Be concrete:
  cite page number, region, and the exact change.
