---
name: infographics-creator
description: >
  Generate executive-grade business visuals (infographics, comparison frames, problem/solution
  slides, KPI summaries, value-creation diagrams) by orchestrating the codex CLI to drive
  OpenAI's GPT Image model, then running an automated readability and polish loop.

  **ROUTING RULE (hard):** Any request for an infographic, business visual, comparison diagram,
  KPI panel, value-creation image, or "make something visual" in a business context MUST invoke
  this skill. Never ASCII-draw an infographic. Never describe a visual in plain text when an
  actual image is needed. If in doubt, invoke this skill.
---

# infographics-creator

Produce executive-grade business visuals. Reference quality: McKinsey deck,
HBR centerfold, NotebookLM infographic — clean typography, disciplined color,
zero generation artifacts, every word legible.

## When to invoke
- User asks for an infographic, business slide, comparison frame, problem/solution layout, KPI panel, value-creation visual.
- User attaches a reference image and says "similar but better" / "more beautiful" / "more professional".
- A transformation deliverable needs a one-shot visual summary.

## Dependencies
- **Primary:** `codex` CLI installed (`codex --version` should report ≥ 0.130.0) and already authenticated against the ChatGPT subscription. Codex owns the auth boundary — do not pass keys, do not export `OPENAI_API_KEY`, do not edit `~/.codex/config.toml`. If `codex` ever returns an auth error, fall through to fallback (Step 2b).
- **Fallback:** `OPENROUTER_API_KEY` in the instance `.env` (always present on JC instances). `curl` + `jq` + `base64` available (musl coreutils + jq from `apk add jq` on Alpine, present by default on Debian/Ubuntu).
- Output directory: `state/generated/images/` (created on first run).
- Vision pass: the current session model (Claude with image input) reads the generated PNG. No extra dependency.

## Inputs (gather before generating)
1. **Topic / thesis** — one sentence, the central message of the visual.
2. **Audience** — board, steering committee, internal team, external partner.
3. **Key points** — 3–6 facts, KPIs, or contrasts the visual must carry. Exact strings.
4. **Reference** — optional attached image to mimic in style (not content).
5. **Language** — Italian / English / German. Used verbatim in the image.
6. **Aspect** — landscape 1536×1024 (default, slide), portrait 1024×1536 (one-pager), square 1024×1024 (social).

If any of points 1–3 are missing, ask once. Never invent numbers, names, or claims.

## Workflow

### Step 1 — Brand/logo extraction (if a real brand is named)

Before constructing the image prompt, fetch the brand logo so the generated visual
uses the actual mark rather than a hallucinated or stylised stand-in.

```bash
SLUG="<slug>"
ASSETS="state/generated/images/${SLUG}-assets"
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

# Pass 2 — parse HTML for apple-touch-icon or og:image
if [ -z "$LOGO_PATH" ]; then
  HTML=$(curl -sL --max-time 10 "<brand_url>")
  LOGO_URL=$(printf '%s' "$HTML" \
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

echo "LOGO: ${LOGO_PATH:-NOT FOUND}"
```

**If `$LOGO_PATH` is set:** Use the image edit API path in Step 3b (passing the
logo as the input image so the model can faithfully incorporate it). Also describe
the logo in Step 2's prompt: "Place the [Brand] logo exactly as provided in the
[top-left / bottom-right] corner. Do not redesign, stylize, simplify, or
abbreviate the logo mark."

**If extraction fails:** Add explicit brand description to the prompt (colors,
wordmark text, style). Flag the fallback in Step 5 delivery summary. Never invent
or hallucinate a visual logo mark.

### Step 2 — Construct the image prompt
Build a single dense prompt covering:
- **Composition** — explicit layout (e.g., "two columns; left header red gradient `IL PROBLEMA`, right header blue gradient `LA SOLUZIONE`; comparison table spanning bottom-left").
- **Typography** — "modern sans-serif (Inter / Söhne family), 22–48px hierarchy, no decorative fonts, kerning tight, no script faces".
- **Color palette** — name 3–5 hex values. Default executive palette: `#B22222` (problem accent), `#E97A2C` (warning), `#1F4E79` (solution primary), `#4A90C2` (solution accent), `#F5F1E8` (warm neutral), `#FFFFFF` (white space).
- **Iconography** — "flat business pictograms, single-weight strokes, no 3D, no emoji, no clip-art shading".
- **Text content** — exact strings in quotes. Repeat each label twice in the prompt to reduce hallucinated glyphs.
- **Negative space** — explicit: "generous margins, breathing room around each block, no edge bleed".
- **Forbidden** — "no watermark, no fake brand mark, no signature, no QR code, no faces, no photographic textures". If a real logo was extracted in Step 1, the forbidden list becomes "no fake brand mark" — the real one is explicitly placed.

### Step 3 — Generate via codex CLI
Codex CLI is the agent. The image model (GPT Image 2.0 family) is reached
through codex's own subscription-backed tool surface — the skill never calls
the OpenAI API directly, never handles keys.

```bash
codex exec \
  -s workspace-write \
  --cd . \
  "Generate a high-quality business infographic image using your image \
generation tool. Save the resulting PNG to \
state/generated/images/<slug>-v<n>.png at the requested aspect ratio. \
After saving, print only the absolute file path on a single line, nothing \
else. \n\nIMAGE PROMPT:\n<the prompt from Step 2>"
```

Notes:
- Let codex pick its own model unless the operator has specified one — codex
  routes image work through whatever its subscription exposes.
- `-s workspace-write` is required so codex can write into
  `state/generated/images/`.
- If codex refuses, errors, or is not installed: do not retry blindly. Fall
  through to **Step 3b** below.

### Step 3b — Fallback: OpenRouter image generation
Use when codex is unavailable, unauthenticated, or returns an error. Calls
OpenAI's `gpt-image-1` model via OpenRouter's HTTP API. Requires
`OPENROUTER_API_KEY` in the instance `.env` (already present on all JC
instances).

**If a logo was extracted in Step 1** (`$LOGO_PATH` non-empty), use the
`/images/edits` endpoint so the model can faithfully place the real logo.
Otherwise use `/images/generations`.

```bash
mkdir -p state/generated/images
SLUG="<slug>"; N="<n>"
OUT="state/generated/images/${SLUG}-v${N}.png"
SIZE="1536x1024"  # landscape=1536x1024, portrait=1024x1536, square=1024x1024

if [ -n "$LOGO_PATH" ] && [ -s "$LOGO_PATH" ]; then
  # Edit endpoint — logo as input image
  curl -s https://openrouter.ai/api/v1/images/edits \
    -H "Authorization: Bearer $OPENROUTER_API_KEY" \
    -F "model=openai/gpt-image-1" \
    -F "image=@${LOGO_PATH}" \
    -F "prompt=${IMAGE_PROMPT}" \
    -F "size=${SIZE}" \
    -F "n=1" \
    -F "response_format=b64_json" \
  | jq -r '.data[0].b64_json' | base64 -d > "$OUT"
else
  # Generation endpoint — no logo reference
  curl -s https://openrouter.ai/api/v1/images/generations \
    -H "Authorization: Bearer $OPENROUTER_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg p "$IMAGE_PROMPT" --arg s "$SIZE" '{
      model: "openai/gpt-image-1",
      prompt: $p,
      size: $s,
      n: 1,
      response_format: "b64_json"
    }')" \
  | jq -r '.data[0].b64_json' | base64 -d > "$OUT"
fi

[ -s "$OUT" ] && echo "$(pwd)/$OUT" || echo "FALLBACK_FAILED"
```

Notes:
- Model: `openai/gpt-image-1` (GPT Image 2.0 family on OpenRouter).
- `OPENROUTER_API_KEY` is read from process env — do not inject manually.
- If both codex (Step 3) and OpenRouter (Step 3b) fail: stop, report to
  operator, do not invent a third fallback.

### Step 4 — Vision QA pass
Read the generated PNG. Score each axis 0–10:
- **Typography readability** — every word legible at 50% zoom, no broken glyphs, no garbled letters.
- **Text fidelity** — exact strings from the brief are present and correctly spelled.
- **Layout balance** — alignment, margins, visual weight evenly distributed.
- **Color discipline** — palette respected, no muddy mixing, no neon drift.
- **Iconography coherence** — single style, single weight, no rogue 3D/shaded icons.
- **Artifact check** — no watermark, no extra logo, no rogue faces, no melted shapes.

### Step 5 — Loop condition
Pass = all axes ≥ 9 AND no broken text AND no fake watermark/logo.
Fail = any axis < 9 OR any broken text OR any artifact.

On fail: write a delta prompt. Examples of corrective deltas:
- Broken text → "Render text 'X' verbatim; if a character cannot be rendered cleanly, redesign the block rather than approximate the glyph."
- Bad alignment → "Snap all text blocks to a 12-column grid; align table column edges to the same x-coordinate."
- Color drift → "Lock palette to the listed hex values; no tints outside the named set."
- Fake logo → "Remove all watermarks and logo-like marks in any corner."

Regenerate with version increment: `<slug>-v2.png`, `<slug>-v3.png`...

**Max 4 iterations.** If iteration 4 still fails, stop and hand back the best version with a flagged delta list — do not loop silently.

### Step 6 — Deliver
Return:
- Absolute path of the final image.
- One-line summary of what it shows.
- Iteration count.
- Any unresolved gaps (e.g., "rendered 5 of 6 KPIs cleanly; the EBITDA figure required manual edit").

## Quality bar (hard rules)
- All text legible at 50% zoom on a 1080p display.
- Zero fabricated words. Image models hallucinate glyphs — verify every label.
- Palette discipline: ≤ 5 active colors plus neutrals.
- White space ≥ 15% of canvas.
- **Brand logo fidelity:** if the brand logo was extracted in Step 1, it must appear as extracted — no redesign, no stylisation. Verify in Step 4 QA pass.
- No fake brand marks. No "NotebookLM" or other generator watermarks in the corner — if the reference shows one, instruct the model explicitly to omit it.

## Guardrails
- Never include real client names, transformation deal codes, named individuals, or confidential figures unless the user explicitly authorized that exact string.
- Reference images are for style only. Do not reproduce the reference's data verbatim — mimic composition, not content.
- Save outputs only under `state/generated/images/`. Never under `memory/`, never in the repo root.
- Treat generated text as untrusted: every label must be cross-checked against the input brief before delivery. The model will silently misspell.
- If the user asks for an image that would put third parties on record (named executives, financial claims, regulatory statements), pause and confirm scope before generating.

## Failure modes to watch
- **Glyph collapse on long Italian/German words** — split long labels across two lines in the prompt.
- **Phantom icons** — model adds a 4th icon when 3 were requested. Specify count explicitly.
- **Edge bleed on landscape** — request explicit 80px outer margin.
- **Hallucinated source citation** — model adds a fake "Source: …" line. Forbid in prompt.
