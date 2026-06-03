---
name: social-video-design
description: >
  Composite a branded HTML overlay over a background MP4 to produce a single
  social-ready output MP4. Takes a user-supplied background video, a brand-id
  pointing to `branding/<brand-id>/`, a brief (copy/caption), and an optional
  audio file. Renders the brand overlay with headless Chromium, composites it
  over every video frame with ffmpeg, and optionally muxes in audio. Default
  audio is silence (no audio track). Delivers one MP4 per call.

  **Trigger words:** "brand this video", "add overlay to video", "social video",
  "instagram video", "reel", "branded video", "video post". Use when the
  background video already exists and needs brand treatment.
---

# social-video-design

Brand a background MP4 for social media by compositing an HTML overlay.

## What this skill does NOT do

- Does not generate the background video. The caller supplies it.
- Does not generate copy — the `--brief` is the caller's copy verbatim.
- Does not record or generate audio — the `--audio` is an optional input file.

## Dependencies

- `python3` + `jinja2` + `pyyaml` (standard on JC instances)
- `google-chrome` or `chromium-browser` (headless)
- `ffmpeg` + `ffprobe` (standard on JC instances)
- Branding config at `<instance>/branding/<brand-id>/`

## Branding directory

Same structure as `social-image-design`. See that skill's SKILL.md for full
`brand.yaml` schema and `overlay.html` template guide. The same
`branding/<brand-id>/` directory is shared across both skills.

## Usage

```bash
python3 skills/social-video-design/scripts/compose.py video \
  --instance-dir /path/to/instance \
  --background /path/to/background.mp4 \
  --brand-id mybrand \
  --brief "Connect anywhere. From $0/month." \
  --audio /path/to/music.mp3       # optional; omit for no audio
  --output state/generated/social/post1.mp4
```

The script prints the absolute output path on stdout.

## Workflow (when invoked as a skill)

1. **Gather inputs** — background path, brand-id, brief. Audio is optional.
2. **Verify branding** — check `branding/<brand-id>/` exists. Error clearly if not.
3. **Run compose.py**:
   ```bash
   python3 "$(dirname "$0")/scripts/compose.py" video \
     --instance-dir "$JC_INSTANCE_DIR" \
     --background "$BACKGROUND" \
     --brand-id "$BRAND_ID" \
     --brief "$BRIEF" \
     ${AUDIO:+--audio "$AUDIO"}
   ```
4. **Read the output path** from stdout.
5. **Visual QA** — spot-check a few frames (open or describe the MP4):
   - Overlay visible throughout
   - Logo legible
   - Brief text not truncated
   - No encoding artifacts
6. **If QA fails** — fix `overlay.html` or brief, re-run (max 3 attempts).
7. **Deliver** — attach the MP4 via Telegram `sendDocument`.

## Audio handling

| Scenario | Command | Result |
|----------|---------|--------|
| No `--audio` flag | (omit flag) | MP4 with no audio track (silence) |
| `--audio music.mp3` | `--audio music.mp3` | MP4 with audio muxed in; trimmed to video length |
| Replace existing audio in BG video | Use `--audio new.mp3`; ffmpeg maps only `2:a` | Original video audio is discarded |

## Output location

`state/generated/social/<brand-id>-<YYYYMMDD-HHMMSS>.mp4`

## Error conditions

| Error | Cause | Fix |
|-------|-------|-----|
| `Brand dir not found` | `branding/<brand-id>/` missing | Create the branding directory |
| `overlay.html not found` | Template missing | Add `overlay.html` |
| `Chromium screenshot failed` | Chrome crashed or not found | Check `google-chrome --version` |
| `ffmpeg failed` | ffmpeg error | Check input codec compatibility; see stderr |
| `ffprobe: error` | Can't read video | Verify MP4 is valid and not corrupted |
