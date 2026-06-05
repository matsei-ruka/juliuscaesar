#!/usr/bin/env bash
# release_update=2026.06.05.1
#
# queue: heartbeat lease renewal stops duplicate dispatch.
#
# Long brain invocations (>lease_seconds, default 300s) tripped
# requeue_expired() mid-call: the row flipped from running back to queued
# while the original worker was still processing, a second slot then
# claimed and dispatched the same event, and the user received two replies.
# Observed when a 17-minute install task produced three copies of the
# completion message.
#
# Fix: queue.renew_lease() + a daemon heartbeat thread in
# _LeaseHeartbeat that bumps locked_until every max(30, lease_seconds/3)
# while process_event runs. Stops itself on rowcount=0 (lease lost).
#
# No schema, no config migration required.
set -euo pipefail

echo "release_update=2026.06.05.1 queue-heartbeat-lease-renewal"
echo "  duplicate-dispatch bug fixed for long brain tasks (>lease_seconds)"
echo "  no instance migration required"
