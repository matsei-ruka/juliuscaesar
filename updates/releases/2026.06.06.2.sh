#!/usr/bin/env bash
# release_update=2026.06.06.2
#
# reply footer: fall back to resumed session id when capture returns None.
#
# After invoke_brain, the runtime now computes
# effective_session_id = result.session_id or resume_session and uses it
# for both _record_session and reply_footer.render_footer. Codex --resume
# appends to the existing JSONL so the snapshot diff returns None; the
# footer previously showed "sess none" even when a real session was active.
#
# No schema, no config migration required.
set -euo pipefail

echo "release_update=2026.06.06.2 reply-footer-fallback-to-resumed-session"
echo "  no instance migration required"
