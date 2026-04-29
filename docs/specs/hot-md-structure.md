# HOT.md Structure & Size Management

## Problem

HOT.md is the rolling 7-day context loaded at every session start. As it grows, it bloats session context window and slows startup. No size bounds.

Current: free-form markdown. Can become 5KB+ (example: current rachel_zane is ~2KB with just 4 sections).

## Solution

Structured HOT.md with hard size limits per section. Overflow rolls to L2 memory (permanent archive). Heartbeat task maintains structure daily.

## Design

### Fixed structure (max 400 lines total)

```markdown
# Hot cache — rolling context

## What shipped (max 5 items, each ≤100 words)
- Item with date, summary, impact

## Immediate open threads (max 5 bullets)
- Thread title + status + blockers

## Known nuisances (max 5 bullets)
- Documented gotchas, workarounds, monitoring alerts
```

### Overflow rules

1. **What shipped:** newest 5 items only. When new item added, oldest drops. Dropped item → L2 entry `memory/L2/completed/{slug}.md`.
2. **Open threads:** when count > 5, trim to 5 newest. Dropped thread → L2 `memory/L2/projects/{slug}.md` (if not already there).
3. **Known nuisances:** persistent until resolved. When resolved, move to L2 `memory/L2/learnings/{slug}.md`.

### Example HOT.md (target size: <300 lines)

```markdown
# Hot cache — rolling context (today 2026-04-29)

## What shipped (last 5)

- **PR #26 (codex-auth-extractor).** Direct OpenAI Responses API access via Codex CLI OAuth. Shipped 2026-04-29. Impact: reduces tokenization cost by 30%, improves latency for heavy-load instances.
- **PR #25 (heartbeat MCP + session continuity).** MCPs now inherited from instance config. Heartbeat workers resume sessions. Shipped 2026-04-27. Impact: enables multi-run context build for long-running tasks.

## Immediate open threads (max 5)

- **FrancescoDatini setup.** Pull + reinstall framework, delete stale `.session` files, smoke-test heartbeat. Blocker: need SSH access.
- **Iran-UAE monitoring resume.** Currently paused. Decision needed: resume or keep paused based on 2026-05-01 threat level.

## Known nuisances (documented)

- Claude CLI auto-updates kill running process. Watchdog now handles; don't panic if process restarts.
- Telegram plugin dies under heavy subprocess load. Watchdog + plugin-death check restarts claude → respawns plugin (~2min dark window).
```

### Archival process

Heartbeat task (daily, e.g., `jc heartbeat run hot_tidy --dry-run`):

1. Read HOT.md sections.
2. Count items in each section.
3. If overflow: extract oldest N items, write to L2 with slug, remove from HOT.md.
4. Rewrite HOT.md with updated header: `# Hot cache — rolling context (today YYYY-MM-DD)`.

Example archival:
```bash
$ jc heartbeat run hot_tidy
HOT.md sections:
  what_shipped:    7 items (max 5) → archiving 2 oldest to memory/L2/completed/
  open_threads:    3 items (within limit)
  known_nuisances: 5 items (at limit)

Archived:
  - memory/L2/completed/pr-24-autonomous-user-model.md (shipped 2026-04-27)
  - memory/L2/completed/gateway-resilience-impl.md (shipped 2026-04-26)

Updated HOT.md (now 254 lines).
```

### Schema for archived items

When moving from HOT.md to L2, create entry:

```markdown
---
name: PR #26 shipped (codex-auth-extractor)
description: Direct OpenAI Responses API access; reduces cost by 30%, improves latency
type: project
shipped: 2026-04-29
impact: production, performance
---

Full details from HOT.md section...

## Follow-up

Monitor cost metrics. If <20% reduction, investigate cache hit rate.
```

## Implementation

1. Add `hot_tidy` task to heartbeat (dry-run by default, `--execute` to commit).
2. Add archival function: `archive_hot_item(section, item, destination)` → writes L2, removes from HOT.md.
3. Update memory tools: `jc memory` CLI shows "recently archived from HOT" in output.
4. Tests: verify section overflow triggers archive, verify L2 entries created correctly, verify HOT.md rewrites preserve structure.

## Bounds

- HOT.md: hard max 400 lines, target <300.
- Each item: max 100 words (enforced on archival).
- Archival: runs daily via heartbeat, deletes oldest first.

## Future

- Compression: L2 entries inherit metadata (shipped date, impact tags) for sorting by "most urgent" or "recent".
- Query: `jc memory recent --section shipped --days 7` → list what shipped in last week.
- Integration with proposal ledger: shipped PR updates `proposals/ideas-log` automatically.
