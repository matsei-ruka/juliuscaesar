#!/usr/bin/env bash
# release_update=2026.06.06.1
#
# codex brain: honor CODEX_HOME when capturing session ids.
#
# lib/gateway/brains/codex.py:_session_root() now reads CODEX_HOME via
# env_value() before defaulting to ~/.codex/. Previously hard-coded to
# Path.home() / ".codex", so instances with CODEX_HOME in .env never
# persisted a sessions row → every dispatch ran resume=no.
#
# No schema, no config migration required.
set -euo pipefail

echo "release_update=2026.06.06.1 codex-session-root-honors-CODEX_HOME"
echo "  no instance migration required"
