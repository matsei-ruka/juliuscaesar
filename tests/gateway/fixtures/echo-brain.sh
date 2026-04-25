#!/usr/bin/env bash
# Fake brain adapter for tests. Echoes stdin to stdout, no model required.
set -eu
exec cat
