"""Release hook for 2026.05.13.01.

This hotfix changes watchdog and gateway-recovery framework behavior. Existing
instances do not need a file migration because the new safeguards are enforced
by code defaults even when older ops/watchdog.yaml files omit the new key.
"""

from __future__ import annotations

import argparse


RELEASE_VERSION = "2026.05.13.01"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"release-update-{RELEASE_VERSION}")
    parser.add_argument("--from-version", default="")
    parser.add_argument("--to-version", default=RELEASE_VERSION)
    parser.add_argument("--instance-dir")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(f"release_update={RELEASE_VERSION}")
    print("watchdog_recovery_replay_guard=framework_default")
    print("long_running_notice_requires_triage=framework_default")
    if args.instance_dir:
        print(f"instance={args.instance_dir}")
    print("release hook complete; no instance file migration required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
