---
name: remotion-video
description: >
  Produce programmatic video renders on the local GPU (no cloud, no AWS, no Lambda).
  Uses Remotion + React/TSX to generate MP4 videos with animations, titles, transitions,
  and motion graphics. Runs entirely on the host machine using Chromium headless + FFmpeg.

  **ROUTING RULE (hard):** Any request containing "create video", "make a clip",
  "generate video", "animated explainer", "social short", "motion graphics",
  "render video", "mp4", aspect-ratio keywords (9:16, 16:9, 1:1), or words like
  "1080p", "4K", "60fps" combined with a video intent MUST invoke this skill.
  Never describe video content in prose when a render-ready video is expected.
  Never ASCII-sketch a video timeline. If in doubt, invoke this skill.
---

# remotion-video

Produce programmatic MP4 videos using Remotion rendered on local GPU hardware.
Zero cloud cost. Zero upload latency. M-series GPU acceleration via Chromium WebCodecs.

## When to invoke
- User asks for a video, clip, animated explainer, social short, motion graphic.
- Keywords: "video", "clip", "mp4", "render", "1080p", "4K", "9:16", "16:9", "1:1", "shorts", "reel".
- A transformation deliverable needs visual explanation.

## Dependencies
- **Remotion project:** `/Users/lucamattei/remotion-jc/` — installed with `remotion`, `@remotion/cli`, `react`, `react-dom`, `typescript`.
- **Entry point:** `src/index.ts` registers all compositions via `RemotionRoot`.
- **FFmpeg:** available at `~/.local/bin/ffmpeg` (installed system-wide).
- **GPU:** M4 Max 40-core with Metal 4. Rendering uses `npx remotion render` with automatic hardware encoding.

## Inputs (gather before rendering)
1. **Topic / brief** — one to three sentences describing the video content.
2. **Duration** — in seconds, max 60 for first version. Default: 15s.
3. **Aspect ratio** — `16:9` (1920×1080, default), `9:16` (1080×1920), or `1:1` (1080×1080).
4. **FPS** — default 30, supported: 24, 30, 60.
5. **Scenes** — list of scenes with approximate timing and copy/visual description.
6. **Music** — yes/no. If yes, source path or royalty-free requirement.
7. **Brand logo** — optional path to PNG/SVG for overlay.

If any of points 1–5 are missing, ask once. Never invent content.

## Workflow

### Step 1 — Write the React/TSX composition

Create a new composition file at `/Users/lucamattei/remotion-jc/src/<slug>.tsx`.

Every composition follows this template:

```tsx
import React from "react";
import { AbsoluteFill, useCurrentFrame, interpolate, spring, useVideoConfig } from "remotion";

export const <PascalCaseName>: React.FC = () => {
  const frame = useCurrentFrame();
  // Animation logic using frame, spring(), interpolate()
  return <AbsoluteFill style={{ /* scene */ }}>{/* elements */}</AbsoluteFill>;
};
```

**Key animation primitives:**
- `useCurrentFrame()` — returns current frame number (0..durationInFrames-1).
- `spring({ frame, fps, config: { damping, mass } })` — spring-based animation (0→1).
- `interpolate(frame, [inStart, inEnd], [outStart, outEnd], { extrapolateRight: "clamp" })` — linear interpolation.
- `AbsoluteFill` — full-canvas positioned div.

**Composition rules:**
- Use `AbsoluteFill` or absolute positioning. No Flexbox layouts within AbsoluteFill for cross-scene consistency.
- Keep text elements under 25 words. Use `fontFamily: "system-ui, sans-serif"` for cross-platform rendering.
- Colors: choose 3–5 hex values. Use CSS `backgroundColor` and `color`, not inline classes.
- For scene transitions: check frame ranges and render conditionally.

### Step 2 — Register the composition

Edit `/Users/lucamattei/remotion-jc/src/Root.tsx` — add the new Composition entry inside the return statement:

```tsx
<Composition
  id="<slug>"
  component={<PascalCaseName />}
  durationInFrames={<fps * duration>}
  fps={<fps>}
  width={<width>}
  height={<height>}
/>
```

### Step 3 — Render

```bash
cd /Users/lucamattei/remotion-jc && \
  source ~/.nvm/nvm.sh && \
  npx remotion render src/index.ts <slug> <slug>.mp4
```

**Rendering notes:**
- First render bundles (slower). Subsequent renders use cache.
- M4 Max renders ~90 frames/sec at 1080p30 with 8x concurrency.
- Output is H.264 MP4 in the `remotion-jc/` directory.

### Step 4 — Deliver

1. The rendered MP4 is at `/Users/lucamattei/remotion-jc/<slug>.mp4`.
2. Upload to WebDAV at `https://dav.omnisage.org/<instance>/videos/<slug>.mp4` if WebDAV is configured for the instance.
3. Deliver via Telegram `sendDocument` if file is <50MB (most renders will be).
4. Provide one-line summary caption: "[Topic] — [duration]s [aspect] video rendered on M4 Max GPU."

## Quality bar
- Text must be legible at native resolution. Minimum font size: 16px at 1080p.
- Animations must complete before scene end. No hung springs.
- Black/dark theme preferred for JC ecosystem. Light theme for presentations.
- Max 4 scenes for ≤30s. Max 6 scenes for ≤60s.
- No text-spilling off canvas bounds.

## Performance notes
- 15s 1080p30 renders in ~5s. 30s renders in ~9s. 60s renders in ~18s.
- 4K renders: approximately 4× slower.
- 60fps renders: approximately 2× slower.
- Cost: $0 (local GPU, no cloud billing).

## Guardrails
- Never include real client names, deal codes, or confidential data unless explicitly authorized.
- The `remotion-jc/` scratch directory should NOT be committed to the JC repo.
- Output MP4s live in `remotion-jc/`. Move delivered files to permanent storage; clean up temp renders.
- Do NOT install or reference `@remotion/lambda` — local GPU only. No AWS, no S3, no Lambda.

## Failure modes
- **Bundle error on first render** — run `npx remotion bundle src/index.ts` first to validate.
- **Memory pressure** — reduce `--concurrency` flag (default is auto / CPU count).
- **Font rendering issues** — stick to `system-ui`, `-apple-system`, or `sans-serif`; avoid webfont `@import`.
- **Audio sync** — if adding music, ensure audio file is same sample rate as video (default 48kHz for FFmpeg).
