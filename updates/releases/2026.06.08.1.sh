#!/usr/bin/env bash
# release_update=2026.06.08.1
#
# Grok adapter bug fixes: MODEL_SWITCH auto-retry + image delivery.
#
# - MODEL_SWITCH_INCOMPATIBLE_AGENT: grok.sh detects the error and retries
#   without -r (fresh session) instead of propagating rc=1 to the recovery
#   classifier. Eliminates the 300s retry loop on model-switch failures.
# - Image delivery: grok.sh probes ~/.grok/sessions/<slug>/<sid>/images/
#   after each run; paths written to sidecar["images"]. BrainResult gains
#   image_paths field. Runtime sends each image via sendPhoto after text reply.
#
# No schema changes. No config migration required.
set -euo pipefail

echo "release_update=2026.06.08.1 grok-model-switch+images"
echo "  grok.sh: MODEL_SWITCH_INCOMPATIBLE_AGENT -> retry fresh session"
echo "  grok.sh: probe images dir, write paths to sidecar"
echo "  base.py: BrainResult.image_paths from sidecar"
echo "  telegram_outbound.py: send_photo() helper"
echo "  runtime.py: deliver image_paths via sendPhoto after text reply"
echo "  no schema change, no config migration required"
