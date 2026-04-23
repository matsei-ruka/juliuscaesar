#!/usr/bin/env bash
# Minimax adapter — STUB. Wire up when needed.
# Most likely implementation: curl to OpenAI-compatible endpoint using
# MINIMAX_API_KEY from the central .env. Sketch:
#
#   curl -sS https://api.minimax.io/v1/chat/completions \
#     -H "Authorization: Bearer $MINIMAX_API_KEY" \
#     -H "Content-Type: application/json" \
#     -d '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"'"$PROMPT"'"}]}' \
#   | jq -r '.choices[0].message.content'
set -euo pipefail
echo "minimax adapter not yet implemented" >&2
exit 127
