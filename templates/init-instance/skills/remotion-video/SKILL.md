---
name: remotion-video
description: >
  Generate motion-graphics videos (animated explainers, social shorts, title sequences,
  product demos, kinetic typography) by writing React/TSX Remotion compositions and
  rendering them locally on the Mac Studio GPU.

  **ROUTING RULE (hard):** Any request containing "create video", "make a clip",
  "animated explainer", "social short", "motion graphics", "video render",
  "9:16 video", "16:9 clip", "TikTok video", "Reel", "YouTube short", or
  "render a video of/with/about" MUST invoke this skill. Never narrate a video
  idea in prose. Never ASCII-sketch a storyboard. If a video is asked for,
  render it.
---

# remotion-video

Generate motion-graphics videos by writing Remotion compositions and rendering
them on the local GPU. Reference quality: Manim-style clean math animations,
Vercel-style product demos, attention-grabbing social shorts — flat design,
smooth springs, crisp typography, zero compression artifacts.

## When to invoke
- User asks for a video, clip, short, reel, animated explainer, kinetic-typography piece.
- User specifies an aspect ratio: 16:9 (YouTube), 9:16 (TikTok/Reels/Shorts), 1:1.
- User asks to "visualize" a concept, demo, or workflow that benefits from motion.
- User says "make this into a video" or "I need a clip for…"

## Dependencies
- **Remotion:** installed at `/Users/lucamattei/remotion-jc/` (npm project with `remotion` and `@remotion/cli`).
- **Node runtime:** `~/.nvm/versions/node/v20.20.1/bin/node`. Always source nvm before remotion commands: `export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh"`.
- **Chrome Headless Shell:** auto-downloaded by Remotion on first render. Already cached.
- **ffmpeg:** `/Users/lucamattei/.local/bin/ffmpeg` (v4.4, h264 + AAC).
- **Hardware:** Apple M4 Max, 64 GB, 16-core GPU. Concurrency: 8x. Render speed: ~50 fps sustained for 1080p compositions.
- **Output directory:** `output/` inside the remotion-jc project. Created on first run.

**No cloud dependencies.** No AWS, no Lambda, no S3. Everything renders locally.

## Inputs (gather before generating)

1. **Topic / thesis** — one sentence describing what the video communicates.
2. **Duration** — in seconds. Default: 15s. Max: 60s (longer renders risk timeouts in agent context).
3. **Aspect ratio** — 16:9 (1920×1080, default), 9:16 (1080×1920), or 1:1 (1080×1080).
4. **Scenes** — 2–5 scene descriptions, each with:
   - On-screen text (exact strings).
   - Visual style (flat shapes, charts, code, icons, kinetic type).
   - Motion direction (slide, scale, fade, spring, typewriter).
5. **Color palette** — 2–4 hex values. If not specified, use the JC dark theme: `#0a0a1a` (bg), `#ffffff` (text), `#4a90c2` (primary accent), `#e97a2c` (secondary accent).
6. **Music** — yes/no. If yes, loop a royalty-free track from `/Users/lucamattei/remotion-jc/assets/audio/` or generate silence. Default: no music.
7. **Brand logo** — path or URL. Placed bottom-right or top-left at 24–48px height with 32px margin.

If inputs 1–3 are missing, ask once. Never invent durations, topics, or copy.

## Base composition template

Every composition file follows this skeleton. The agent writes the scene components and registers them with `registerRoot`.

```tsx
import React from 'react';
import {
  Composition, useCurrentFrame, useVideoConfig,
  interpolate, spring, AbsoluteFill, Sequence,
  registerRoot,
} from 'remotion';

// ── Scene components go here ──

const Root: React.FC = () => {
  return (
    <>
      <Composition
        id="<slug>"
        component={MainComposition}
        durationInFrames={<totalFrames>}
        fps={30}
        width={<width>}
        height={<height>}
      />
    </>
  );
};

registerRoot(Root);
```

**Available imports from `remotion`:** `useCurrentFrame`, `useVideoConfig`, `interpolate`, `spring`, `AbsoluteFill`, `Sequence`, `Series`, `Img`, `Audio`, `OffthreadVideo`, `Easing`, `Loop`, `MeasureSpring`, `random`.

**Style notes:**
- All styles are inline React CSS objects. No CSS files, no Tailwind.
- Fonts: system sans-serif stack (`-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`). Monospace for code: `'SF Mono', 'Fira Code', monospace`.
- No external font loading — system fonts only for render speed.
- `AbsoluteFill` is the default container: `position: absolute, top/left/right/bottom: 0`.

## Workflow

### Step 1 — Write the composition

Write a single TSX file at `src/compositions/<slug>.tsx` inside the Remotion
project. The file must:
- Define 2–5 scene components using Remotion hooks.
- Sequence them with `<Sequence from={…} durationInFrames={…}>`.
- Export a `<Composition>` via `registerRoot`.
- Use only inline styles and the imports listed above.

Keep the file under 150 lines. Remotion renders frame-by-frame — heavy per-frame
computation or large data structures slow things down. Prefer `spring()` over
raw `interpolate()` for natural motion.

### Step 2 — Render the video

```bash
export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh"
cd /Users/lucamattei/remotion-jc
mkdir -p output
npx remotion render src/compositions/<slug>.tsx <slug> output/<slug>.mp4
```

Render time estimator: ~50 fps on M4 Max for 1080p. A 15-second (450-frame)
video takes ~9 seconds. A 60-second (1800-frame) video takes ~36 seconds.

Add `--concurrency=16` to push harder on the GPU for longer renders.

If rendering fails with a bundling error, check:
- `registerRoot` is called at the bottom of the file.
- `<Composition>` is inside the `Root` component, not at module scope.
- All imports are from `'remotion'` (not `'react'` for hooks — though `useState`/`useEffect` from React are fine).

### Step 3 — Verify output

```bash
ls -lh output/<slug>.mp4
```

File size for 15s 1080p h264 at CRF 18: ~800 KB – 2 MB depending on motion
complexity. If the file is under 10 KB, the render likely failed silently —
re-check the composition for runtime errors.

### Step 4 — Deliver

1. **Upload to WebDAV** if `WEBDAV_URL` and `WEBDAV_CREDS` are in the instance `.env`:
   ```
   curl -T output/<slug>.mp4 \
     -u "$WEBDAV_CREDS" \
     "$WEBDAV_URL/<instance>/videos/<slug>.mp4"
   ```
   Return the URL: `https://dav.omnisage.org/<instance>/videos/<slug>.mp4`

2. **Deliver via Telegram** using the `sendDocument` tool if filesize < 50 MB.
   Caption: one-line summary of the video (topic + duration).
   If > 50 MB, deliver the WebDAV URL only.

3. **If neither WebDAV nor Telegram is available**, return the local path:
   `/Users/lucamattei/remotion-jc/output/<slug>.mp4`.

## Quality bar (hard rules)

- Motion must be smooth — no stutter, no frame drops. Remotion renders
  server-side so this is about correct easing, not hardware.
- All text must be legible at playback speed. Minimum font size: 18px for
  1080p, 24px for 720p (9:16 shorts).
- No placeholder text. Every string must match the user's input exactly.
- No fake brand marks, no watermarks, no "Made with Remotion" badges.
- Aspect ratio must match the user's request. Never render 16:9 when they
  asked for 9:16.
- FPS locked at 30. Higher frame rates double render time with diminishing
  returns for motion graphics.

## Guardrails
- Never render a video with unconfirmed copy. If the user says "something like…",
  ask for exact text before rendering.
- Never include real client names, deal codes, named individuals, or confidential
  figures unless the user explicitly authorized that exact string.
- Save compositions only under `src/compositions/`. Videos only under `output/`.
- Composition files are disposable — they can be deleted after successful delivery.
  The `output/` directory accumulates; clean it periodically.
- If a render takes longer than 90 seconds, it may time out in the agent context.
  For long renders (60s+), warn the user and offer to run it asynchronously.
