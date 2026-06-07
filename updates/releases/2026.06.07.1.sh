#!/usr/bin/env bash
# release_update=2026.06.07.1
#
# Context-aware session lifecycle bug fixes (PR #85 follow-up).
#
# - Triage capacity guard: override rule matching fixed (brain+model_prefix,
#   not full "claude:sonnet" string). Override now returns valid brain name
#   for invoke_brain registry lookup.
# - Routing pressure gate: profile lookup uses exact-first strategy so
#   operator model names (e.g. "small") resolve correctly; ROTATE and FAIL
#   actions are now handled (were silently ignored before this release).
# - Profiles: claude-opus-4-7-1m standard+extended added; claude-opus-4-8
#   1M extended enabled by default; gpt-5.5 Pro 272K added.
#
# No schema changes. No config migration required.
# session_lifecycle: remains opt-in (enabled: false by default).
set -euo pipefail

echo "release_update=2026.06.07.1 context-lifecycle-bugfixes"
echo "  triage_capacity_guard: brain+prefix matching, valid brain return"
echo "  _apply_routing_pressure: ROTATE + FAIL actions now handled"
echo "  profile lookup: exact-first, claude- prefix fallback only"
echo "  profiles: opus-4-7-1m 1M, opus-4-8 1M, gpt-5.5-pro 272K"
echo "  no schema change, no config migration required"
