#!/usr/bin/env bash
# release_update=2026.06.03.1
#
# MarkdownV2 nested-entity fix (PR #84).
#
# `lib/gateway/format/escaper.py` now drops the outer bold/italic/strike
# wrapper when the inner span is a sole code/link placeholder. Telegram
# does not allow code/pre entities to nest inside other formatting; the
# previous behavior produced invalid output that Telegram silently dropped.
#
# Code-only change. No schema, no config, no cron. Gateway restart picks
# up the new escaper.
set -euo pipefail

echo "release_update=2026.06.03.1 markdownv2-escaper-fix"
echo "  escaper: nested code/link inside bold/italic/strike now drops outer wrapper"
echo "  no instance migration required"
