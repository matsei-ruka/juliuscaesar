# Spec: social-image-design + social-video-design skills

**Branch:** `feature/social-content-skills`  
**Status:** approved — implement immediately

## Problem

Agents need to produce branded Instagram (and general social) content by layering design
overlays over AI-generated or user-supplied background media. Two distinct output types:
static images (PNG) and video (MP4).

The skills are "design-over-background" tools — they do not generate the background.
The background (PNG or MP4) is always an explicit input, produced in a prior interaction
or a super-skill that orchestrates the full workflow.

---

## Skill 1 — `social-image-design`

### Purpose

Composite a branded HTML overlay over a background PNG, producing a single output PNG.
The overlay (brand chrome, text, logo, colors) is driven by a per-brand Jinja2 template
and rendered with headless Chromium.

### Inputs

| Param | Required | Description |
|-------|----------|-------------|
| `--background <path>` | yes | Background PNG (any size) |
| `--brand-id <id>` | yes | Branding config folder under `branding/<id>/` |
| `--brief <text>` | yes | Copy/caption/CTA for this specific post |
| `--output <path>` | no | Final PNG path. Default: `state/generated/social/<slug>-<timestamp>.png` |

### Branding structure

```
<instance>/branding/
  <brand-id>/
    brand.yaml      # name, colors, fonts, logo_position, extra vars
    logo.svg        # brand logo (SVG). Fallback: logo.png
    overlay.html    # Jinja2 template rendered over the background
```

Multiple brand-ids per instance are supported. The skill never writes to `branding/`.

`brand.yaml` minimal schema:
```yaml
name: "Brand Name"
colors:
  primary: "#0066CC"
  secondary: "#FF6600"
  text_light: "#FFFFFF"
  text_dark: "#111111"
  overlay_bg: "rgba(0,0,0,0.5)"
fonts:
  heading: "Inter"
  body: "Inter"
logo_position: bottom_right   # top_left | top_right | bottom_left | bottom_right | center
```

`overlay.html` receives this Jinja2 context:
```
{{ brand }}          — brand.yaml dict
{{ logo_svg }}       — raw SVG content (safe to use with |safe filter)
{{ logo_b64 }}       — base64-encoded logo for <img src="data:..."> fallback
{{ logo_ext }}       — "svg" or "png"
{{ brief }}          — user brief string
{{ width }}          — background image width in px
{{ height }}         — background image height in px
```

### Pipeline

```
background.png
      │
      ▼
[1] detect dimensions (Pillow)
      │
      ▼
[2] render overlay.html via Jinja2 → tmp/overlay.html
      │
      ▼
[3] Chromium headless screenshot
    --window-size=WxH --transparent-background
    → tmp/overlay_raw.png (RGBA, same dims as background)
      │
      ▼
[4] Pillow: paste overlay_raw.png (RGBA) over background.png → output.png
      │
      ▼
[5] Impeccable critique loop (max 3 iters)
    — on fail: adjust overlay_html vars, re-render, re-shoot, re-composite
      │
      ▼
[6] deliver path + summary
```

### Output

`state/generated/social/<brand-id>-<timestamp>.png`

One PNG per call. Caller manages carousel assembly.

---

## Skill 2 — `social-video-design`

### Purpose

Composite a branded HTML overlay over a background MP4, producing a single output MP4.
Same overlay pipeline as `social-image-design`; adds ffmpeg compositing and optional audio.

### Inputs

| Param | Required | Description |
|-------|----------|-------------|
| `--background <path>` | yes | Background MP4 |
| `--brand-id <id>` | yes | Branding config folder |
| `--brief <text>` | yes | Copy/caption for this post |
| `--audio <path>` | no | Audio MP3 to mux in. Default: silence (no audio track) |
| `--output <path>` | no | Final MP4 path. Default: `state/generated/social/<slug>-<timestamp>.mp4` |

### Branding structure

Identical to `social-image-design`. The same `branding/<brand-id>/` is reused.

### Pipeline

```
background.mp4
      │
      ▼
[1] probe video: WxH + duration (ffprobe)
      │
      ▼
[2-3] identical to image skill → overlay_raw.png (WxH, RGBA)
      │
      ▼
[4] ffmpeg: overlay over every frame
    -filter_complex "[0:v][1:v]overlay=0:0[v]" -map "[v]"
      │
      ▼
[5a] if --audio supplied: mux audio track (-c:a aac -shortest)
[5b] if no --audio: no audio track (silence = absence of audio)
      │
      ▼
[6] deliver path + summary
```

No Impeccable critique loop for video (frame render is deterministic once overlay PNG is right).
Critique applies to the overlay PNG before ffmpeg compositing if desired.

### Output

`state/generated/social/<brand-id>-<timestamp>.mp4`

One MP4 per call. Audio is optional input; default is no audio track.

---

## Implementation notes

### Scripts

Both skills share a single Python entry-point `scripts/compose.py` dispatching on
`--mode image|video`. The Jinja2 rendering and Chromium screenshot logic is shared.

Dependencies (all available on this host):
- `python3` + `Pillow` + `jinja2` + `pyyaml` (stdlib otherwise)
- `google-chrome` at `/usr/bin/google-chrome`
- `ffmpeg` at `/usr/bin/ffmpeg`
- `ffprobe` (ships with ffmpeg)

### Chromium invocation

```bash
google-chrome \
  --headless=new \
  --no-sandbox \
  --disable-gpu \
  --window-size=WxH \
  --hide-scrollbars \
  --default-background-color=00000000 \
  --screenshot=overlay_raw.png \
  "file:///abs/path/to/overlay.html"
```

The `--default-background-color=00000000` flag makes the viewport transparent — prerequisite
for clean RGBA composite over any background.

### Error handling

- Missing brand dir → hard stop, clear error.
- Missing `overlay.html` → hard stop, clear error.
- Chromium screenshot 0 bytes → hard stop, report.
- ffmpeg error → surface stderr, hard stop.
- Never silently fall back to a text-only output when an image/video was requested.

---

## File layout (post-implementation)

```
juliuscaesar/
  templates/init-instance/skills/
    social-image-design/
      SKILL.md
      scripts/
        compose.py
    social-video-design/
      SKILL.md
      (symlinks or copies compose.py)

rachel_zane/
  skills/
    social-image-design/    (deployed copy)
    social-video-design/    (deployed copy)
  branding/
    example/               (starter template)
      brand.yaml
      logo.svg
      overlay.html
```
