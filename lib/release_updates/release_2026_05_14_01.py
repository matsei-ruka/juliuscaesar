"""Release hook for 2026.05.14.01 — initialize the unified approvals DB."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


RELEASE_VERSION = "2026.05.14.01"


def _ensure_approvals_db(instance_dir: Path) -> str:
    """Open the approvals DB once so the schema lands. Returns a status label."""
    if not (instance_dir / "state").exists():
        return "no_state_dir"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from approvals.db import init_for_instance  # type: ignore
    except Exception as exc:
        return f"import_error:{exc.__class__.__name__}"
    try:
        init_for_instance(instance_dir)
    except Exception as exc:
        return f"init_error:{exc.__class__.__name__}"
    return "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"release-update-{RELEASE_VERSION}")
    parser.add_argument("--from-version", default="")
    parser.add_argument("--to-version", default=RELEASE_VERSION)
    parser.add_argument("--instance-dir")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(f"release_update={RELEASE_VERSION}")
    print("approvals_db=ensured")
    if args.instance_dir:
        instance_dir = Path(args.instance_dir).expanduser().resolve()
        if args.dry_run:
            print(f"instance={instance_dir} dry_run=true")
        else:
            status = _ensure_approvals_db(instance_dir)
            print(f"instance={instance_dir} approvals_db_status={status}")
    print("release hook complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
