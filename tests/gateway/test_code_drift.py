"""Tests for the gateway code-drift self-restart check.

When the framework code on disk is updated past process startup time, the
gateway holds stale in-memory modules and silently breaks every dispatch
(e.g. `TypeError: unexpected keyword argument 'slot'` after a parallel-slots
upgrade). `_check_code_drift` detects this and raises SystemExit so the
watchdog can respawn with fresh code.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway import queue  # noqa: E402
from gateway.config import render_default_config  # noqa: E402
from gateway.runtime import (  # noqa: E402
    GatewayRuntime,
    _lib_dir_newest_mtime,
)


def _instance() -> Path:
    root = Path(tempfile.mkdtemp(prefix="jc-drift-"))
    (root / ".jc").write_text("", encoding="utf-8")
    (root / "ops").mkdir()
    (root / "ops" / "gateway.yaml").write_text(
        render_default_config(default_brain="claude:sonnet-4-6"),
        encoding="utf-8",
    )
    return root


def _build_runtime(instance: Path) -> GatewayRuntime:
    return GatewayRuntime(
        instance,
        log_path=queue.queue_dir(instance) / "test.log",
        stop_requested=lambda: True,
    )


class LibDirNewestMtimeTests(unittest.TestCase):
    def test_returns_newest_py_file_under_tree(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="jc-mtime-"))
        (root / "a.py").write_text("# a", encoding="utf-8")
        sub = root / "sub"
        sub.mkdir()
        target = sub / "b.py"
        target.write_text("# b", encoding="utf-8")
        future = time.time() + 1000
        import os as _os
        _os.utime(target, (future, future))
        mtime, name = _lib_dir_newest_mtime(root)
        self.assertEqual(name, "b.py")
        self.assertGreater(mtime, time.time() + 500)

    def test_empty_dir_returns_zero(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="jc-mtime-empty-"))
        self.assertEqual(_lib_dir_newest_mtime(root), (0.0, ""))

    def test_missing_dir_returns_zero(self) -> None:
        self.assertEqual(_lib_dir_newest_mtime(Path("/nonexistent/xyz/123")), (0.0, ""))


class CheckCodeDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instance = _instance()
        self.runtime = _build_runtime(self.instance)

    def tearDown(self) -> None:
        self.runtime.close()

    def test_no_drift_returns_silently(self) -> None:
        # Backdate startup so any "recently edited" framework files (e.g.
        # this test run) don't trip the drift check during the no-drift
        # baseline assertion.
        self.runtime._startup_time = time.time() + 10
        self.runtime._check_code_drift()

    def test_throttle_skips_within_60s(self) -> None:
        # First call primes _code_drift_last_check. Second call must short-
        # circuit on the throttle, never reaching the mtime scan — so even a
        # planted past-dated file under the lib dir cannot trip it.
        self.runtime._startup_time = time.time() + 10  # baseline: no drift
        self.runtime._check_code_drift()
        import os as _os
        target = self.runtime._lib_dir / "gateway" / "runtime.py"
        self.runtime._startup_time = time.time() - 7200  # would now trigger
        planted = time.time() - 30
        _os.utime(target, (planted, planted))
        try:
            self.runtime._check_code_drift()
        except SystemExit:
            self.fail("throttle should suppress second check within 60s")

    def test_drift_raises_systemexit(self) -> None:
        import os as _os
        # Force throttle window expired.
        self.runtime._code_drift_last_check = 0.0
        # Plant a file dated *between* startup and now, simulating a code
        # update that happened after the process began. Must be in the past
        # to avoid the future-skew guard.
        target = self.runtime._lib_dir / "gateway" / "sessions.py"
        # Backdate startup so the future-skew guard ignores the planted file
        # only if it lies in the past relative to now.
        self.runtime._startup_time = time.time() - 7200
        planted = time.time() - 30
        _os.utime(target, (planted, planted))
        with self.assertRaises(SystemExit) as ctx:
            self.runtime._check_code_drift()
        self.assertEqual(ctx.exception.code, 42)

    def test_future_dated_file_does_not_fire(self) -> None:
        import os as _os
        self.runtime._code_drift_last_check = 0.0
        target = self.runtime._lib_dir / "gateway" / "sessions.py"
        future = time.time() + 7200
        _os.utime(target, (future, future))
        # Should NOT raise — future-dated files are filesystem skew, not drift.
        self.runtime._check_code_drift()


if __name__ == "__main__":
    unittest.main()
