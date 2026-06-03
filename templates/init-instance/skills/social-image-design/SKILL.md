---
name: social-image-design
description: >
  Composite a branded HTML overlay over a background PNG to produce a single
  social-ready output PNG. Takes a user-supplied background image (AI-generated
  or photographed), a brand-id pointing to a branding config at
  `branding/<brand-id>/`, and a brief (copy/caption/CTA). Renders the brand
  overlay with headless Chromium and composites it over the background using
  Pillow. Delivers one PNG per call — caller assembles carousels.

  **Trigger words:** "brand this image", "add overlay", "social image", "instagram
  post", "social post", "branded content", "post image". Use when the background
  already exists and needs brand treatment.
---

# social-image-design

Brand a background PNG for social media by compositing an HTML overlay.

## What this skill does NOT do

- Does not generate the background image. The caller supplies it.
- Does not generate copy — the `--brief` is the caller's copy verbatim.
- Does not produce carousels — one PNG per call; call N times for N slides.

## Dependencies

- `python3` + `Pillow` + `jinja2` + `pyyaml` (standard on JC instances)
- `google-chrome` or `chromium-browser` (headless)
- Branding config at `<instance>/branding/<brand-id>/`

## Setup — branding directory

Create `branding/<brand-id>/` in the instance directory with three files:

### `brand.yaml`
```yaml
name: "Brand Name"
tagline: "Tagline here"
colors:
  primary: "#0066CC"
  secondary: "#FF6600"
  text_light: "#FFFFFF"
  text_dark: "#111111"
  overlay_bg: "rgba(0, 0, 0, 0.55)"
fonts:
  heading: "Inter"
  body: "Inter"
logo_position: bottom_right   # top_left | top_right | bottom_left | bottom_right | center
```

Any extra keys are passed through to `overlay.html` as `{{ brand.your_key }}`.

### `logo.svg` (or `logo.png`)
Place the brand logo here. SVG preferred (scales perfectly).

### `overlay.html`
Jinja2 template rendered over the background. Available template variables:
```
{{ brand }}         — dict from brand.yaml
{{ logo_svg }}      — raw SVG content (use with |safe filter)
{{ logo_b64 }}      — base64-encoded logo bytes
{{ logo_ext }}      — "svg" or "png"
{{ brief }}         — user brief string
{{ width }}         — background width in px
{{ height }}        — background height in px
```

The document body must be **fully transparent** by default (`background: transparent`).
Only the overlay elements should be opaque/semi-opaque.

Example minimal template:
```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      width: {{ width }}px;
      height: {{ height }}px;
      background: transparent;
      font-family: '{{ brand.fonts.heading }}', Inter, sans-serif;
    }
    .logo-wrap {
      position: absolute;
      bottom: 40px;
      right: 40px;
    }
    .logo-wrap img { height: 48px; }
    .brief {
      position: absolute;
      bottom: 110px;
      left: 40px;
      right: 40px;
      color: {{ brand.colors.text_light }};
      font-size: 28px;
      font-weight: 700;
      text-shadow: 0 2px 8px rgba(0,0,0,0.7);
    }
  </style>
</head>
<body>
  <div class="brief">{{ brief }}</div>
  <div class="logo-wrap">
    {% if logo_ext == "svg" %}
      {{ logo_svg|safe }}
    {% else %}
      <img src="data:image/{{ logo_ext }};base64,{{ logo_b64 }}" alt="{{ brand.name }}">
    {% endif %}
  </div>
</body>
</html>
```

## Usage

```python
# Via the compose.py script:
python3 skills/social-image-design/scripts/compose.py image \
  --instance-dir /path/to/instance \
  --background /path/to/background.png \
  --brand-id mybrand \
  --brief "Connect anywhere. From $0/month." \
  --output state/generated/social/post1.png
```

The script prints the absolute output path on stdout.

## Workflow (when invoked as a skill)

1. **Gather inputs** — background path, brand-id, brief. If any missing, ask once.
2. **Verify branding** — check `branding/<brand-id>/` exists with `brand.yaml` + `overlay.html`. Error clearly if not.
3. **Run compose.py**:
   ```bash
   python3 "$(dirname "$0")/scripts/compose.py" image \
     --instance-dir "$JC_INSTANCE_DIR" \
     --background "$BACKGROUND" \
     --brand-id "$BRAND_ID" \
     --brief "$BRIEF"
   ```
4. **Read the output path** from stdout.
5. **Visual QA** — open the output PNG, verify:
   - Logo is present and legible
   - Brief text renders correctly, not truncated
   - Overlay is properly composited (not misaligned, not opaque over full frame)
   - No Chromium debug artifacts in corners
6. **If QA fails** — adjust `overlay.html` or brief, re-run (max 3 attempts).
7. **Deliver** — attach the PNG via Telegram `sendDocument`.

## Output location

`state/generated/social/<brand-id>-<YYYYMMDD-HHMMSS>.png`

## Error conditions

| Error | Cause | Fix |
|-------|-------|-----|
| `Brand dir not found` | `branding/<brand-id>/` missing | Create the branding directory |
| `overlay.html not found` | Template missing | Add `overlay.html` to the brand dir |
| `Chromium screenshot failed` | Chrome not found or crashed | Check `google-chrome --version`; install Chrome |
| 0-byte screenshot | Transparent-only page | Check HTML has visible elements |
| `Missing pyyaml/jinja2/Pillow` | Python deps | `pip install pyyaml jinja2 Pillow` |
