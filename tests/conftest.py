"""pytest conftest — bootstrap sys.path and resolve module name collisions.

The framework's `lib/watchdog/` package shares a name with the upstream
PyPI `watchdog` (filesystem events) commonly installed on dev hosts. We
prepend `lib/` to sys.path and pop any preloaded upstream `watchdog.*`
modules so test imports resolve to our local copy.

Same for `lib/gateway/` — kept here so test files don't repeat the boilerplate.
"""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib"


def _prepend_path(path: Path) -> None:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


_prepend_path(LIB)


def _drop_foreign(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            mod = sys.modules.get(name)
            path = getattr(mod, "__file__", "") or ""
            if not path.startswith(str(LIB)):
                sys.modules.pop(name, None)


_drop_foreign("watchdog")
