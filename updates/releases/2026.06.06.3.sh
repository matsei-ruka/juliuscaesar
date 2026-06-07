#!/usr/bin/env bash
# release_update=2026.06.06.3
#
# Context-aware session lifecycle (spec docs/specs/context-aware-session-lifecycle.md,
# PR #85). Ships §8 telemetry, §9 profiles + config, §11 routing pressure gate,
# §17 LLM context recovery, §18 /compact + operator notification, §19 observability.
#
# New schema: session_lifecycle table added to queue.db on first use via
# telemetry.init_db() (called lazily at runtime and during /compact).
# No manual migration required — the table is created automatically.
#
# New config keys (all opt-in, safe to ignore):
#   session_lifecycle:
#     enabled: false        # set true to activate routing pressure + telemetry
#   compaction_notify:
#     enabled: true         # operator notification on /compact (on by default)
#
# No existing config keys removed or renamed.
set -euo pipefail

echo "release_update=2026.06.06.3 context-aware-session-lifecycle"
echo "  new: session_lifecycle table (auto-created), session_lifecycle: config block"
echo "  new: compaction_notify: config block (enabled by default)"
echo "  new: /compact slash command"
echo "  new: context_exhausted + context_profile_unavailable recovery handlers"
echo "  no breaking changes, no required migration"
