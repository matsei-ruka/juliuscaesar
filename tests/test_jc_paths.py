from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jc_paths import InstanceResolutionError, resolve_instance_dir, resolve_instance_path


class InstanceResolutionTests(unittest.TestCase):
    def test_arg_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self.assertEqual(resolve_instance_dir(instance), instance.resolve())

    def test_env_used_when_arg_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            with mock.patch.dict(os.environ, {"JC_INSTANCE_DIR": str(instance)}):
                self.assertEqual(resolve_instance_dir(), instance.resolve())

    def test_walks_up_for_jc_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / ".jc").write_text("", encoding="utf-8")
            nested = instance / "a" / "b"
            nested.mkdir(parents=True)
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(resolve_instance_dir(cwd=nested), instance.resolve())

    def test_cwd_marker_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "heartbeat").mkdir()
            (instance / "heartbeat" / "tasks.yaml").write_text("tasks: {}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    resolve_instance_dir(
                        cwd=instance,
                        fallback_markers=("heartbeat/tasks.yaml",),
                    ),
                    instance.resolve(),
                )

    def test_missing_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(InstanceResolutionError):
                    resolve_instance_dir(cwd=Path(tmp), fallback_markers=("memory",))


class SafePathTests(unittest.TestCase):
    def test_rejects_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "inst"
            instance.mkdir()
            with self.assertRaises(ValueError):
                resolve_instance_path(instance, "../outside")


if __name__ == "__main__":
    unittest.main()
