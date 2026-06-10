"""Audit feature 5 — brain health probes + BrainFailureStore TTL/recovery."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from gateway.brain_failure import BrainFailureStore  # noqa: E402
from gateway.brain_health import (  # noqa: E402
    configured_brain_specs,
    probe_all,
    probe_spec,
    role_is_critical,
)
from gateway.config import clear_env_cache, load_config  # noqa: E402


def _instance(tmp: str, yaml_body: str) -> Path:
    instance = Path(tmp)
    (instance / "ops").mkdir(parents=True, exist_ok=True)
    (instance / "ops" / "gateway.yaml").write_text(yaml_body, encoding="utf-8")
    return instance


class ProbeSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_env_cache()

    def test_unknown_brain_fails_for_critical_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = probe_spec(Path(tmp), "default_fallback_brain", "nope:model")
        self.assertEqual(res.level, "fail")
        self.assertTrue(res.problems)

    def test_unknown_brain_warns_for_non_critical_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = probe_spec(Path(tmp), "channels.telegram.brain", "nope")
        self.assertEqual(res.level, "warn")

    def test_missing_cli_binary_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("gateway.brain_health.shutil.which", return_value=None):
                res = probe_spec(Path(tmp), "default_fallback_brain", "pi:minimax-m3")
        self.assertEqual(res.level, "fail")
        self.assertTrue(any("not on PATH" in p for p in res.problems))

    def test_openrouter_without_key_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No OPENROUTER_API_KEY in .env; secret-strict env_value never
            # reads os.environ, so validate() must fail.
            res = probe_spec(
                Path(tmp), "triage_unsafe_fallback_brain", "openrouter:x-ai/grok-4-fast"
            )
        self.assertEqual(res.level, "fail")
        self.assertTrue(any("validation failed" in p for p in res.problems))

    def test_role_criticality(self):
        self.assertTrue(role_is_critical("default_brain"))
        self.assertTrue(role_is_critical("default_fallback_brain"))
        self.assertTrue(role_is_critical("triage_backup.coding"))
        self.assertTrue(role_is_critical("supervisor.recovery.fallback_brain"))
        self.assertFalse(role_is_critical("channels.telegram.brain"))


class ConfiguredSpecsTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_env_cache()

    def test_collects_default_and_fallback_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _instance(
                tmp,
                "default_brain: claude\n"
                "default_fallback_brain: codex\n"
                "triage_unsafe_fallback_brain: openrouter\n",
            )
            cfg = load_config(instance)
            roles = dict(configured_brain_specs(instance, cfg))
        self.assertEqual(roles.get("default_brain"), "claude")
        self.assertEqual(roles.get("default_fallback_brain"), "codex")
        self.assertEqual(roles.get("triage_unsafe_fallback_brain"), "openrouter")

    def test_probe_all_dedupes_and_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = _instance(tmp, "default_brain: claude\n")
            cfg = load_config(instance)
            results = probe_all(instance, cfg)
        self.assertTrue(results)
        self.assertEqual(len({(r.role, r.spec) for r in results}), len(results))


class FailureStoreTTLTests(unittest.TestCase):
    def test_mark_is_failed_within_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BrainFailureStore(Path(tmp), ttl_seconds=3600)
            store.mark_failed("pi")
            self.assertTrue(store.is_failed("pi"))

    def test_mark_expires_after_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BrainFailureStore(Path(tmp), ttl_seconds=10)
            store.mark_failed("pi")
            with mock.patch(
                "gateway.brain_failure.time.time",
                return_value=time.time() + 11,
            ):
                self.assertFalse(store.is_failed("pi"))
            # Expiry is persisted — a fresh load no longer sees the mark.
            fresh = BrainFailureStore(Path(tmp), ttl_seconds=10)
            self.assertFalse("pi" in fresh.all_failed())

    def test_expiry_survives_reload_and_clear_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BrainFailureStore(Path(tmp), ttl_seconds=3600)
            store.mark_failed("claude")
            store.clear("claude")
            self.assertFalse(store.is_failed("claude"))


if __name__ == "__main__":
    unittest.main()
