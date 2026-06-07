#!/usr/bin/env bash
# release_update=2026.06.07.2
#
# opencode v2 adapter, grok adapter, codex audit fixes, supervisor lock.
#
# - opencode v2 (PR #86): sessionID from first step_start, token usage from
#   opencode.db, sidecar reader in base.py.
# - grok adapter (PR #87): GrokBrain + grok.sh, NDJSON parse, sidecar token
#   telemetry, --system-prompt-override for L1 preamble.
# - Codex audit fixes (PR #88): adapter stderr isolation, got_end gate,
#   truncation warning, prompt file to bypass -p clap bug.
# - Supervisor concurrent-tick lock.
#
# No schema changes. No config migration required.
set -euo pipefail

echo "release_update=2026.06.07.2 opencode-v2+grok+fixes"
echo "  opencode: v2 adapter, sessionID from step_start, usage from opencode.db"
echo "  grok: GrokBrain + grok.sh, NDJSON parse, sidecar telemetry"
echo "  grok: --system-prompt-override for L1 preamble"
echo "  fixes: adapter stderr isolation, got_end gate, truncation warning"
echo "  supervisor: concurrent-tick lock"
echo "  no schema change, no config migration required"
