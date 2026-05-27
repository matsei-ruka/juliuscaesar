"""Tests for ``lib/company/runtime.py`` introspection helpers + reporter
integration of the ``runtime`` block.

Spec: ``docs/specs/reporter-runtime-snapshot.md``.

Each helper has:
  - a happy-path test
  - at least one failure-mode test (monkeypatch the underlying call
    to raise; assert ``None`` rather than propagation)

Plus one integration test on ``Reporter.snapshot()`` to confirm the
block is attached to the gateway snapshot payload with the expected
shape.
"""

from __future__ import annotations

import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from company import runtime as runtime_helpers  # noqa: E402
from company.reporter import Reporter  # noqa: E402
from gateway import config as gw_config  # noqa: E402


# --------------------------------------------------------------------------- #
# hostname()                                                                  #
# --------------------------------------------------------------------------- #


class HostnameTests(unittest.TestCase):
    def test_happy_path_returns_socket_gethostname(self) -> None:
        with patch.object(socket, "gethostname", return_value="noah-bitwell"):
            self.assertEqual(runtime_helpers.hostname(), "noah-bitwell")

    def test_returns_none_when_gethostname_raises(self) -> None:
        with patch.object(socket, "gethostname", side_effect=OSError("nss down")):
            self.assertIsNone(runtime_helpers.hostname())

    def test_empty_hostname_becomes_none(self) -> None:
        """Some containers report ''. Treat it as no-info."""
        with patch.object(socket, "gethostname", return_value=""):
            self.assertIsNone(runtime_helpers.hostname())


# --------------------------------------------------------------------------- #
# primary_ip()                                                                #
# --------------------------------------------------------------------------- #


class PrimaryIpTests(unittest.TestCase):
    def test_happy_path_returns_udp_socket_getsockname_ip(self) -> None:
        """UDP trick: connect() sets the routing lookup, getsockname()
        reveals the interface IP. No packet leaves the host."""
        fake_sock = MagicMock()
        fake_sock.getsockname.return_value = ("192.168.14.112", 51234)

        with patch.object(socket, "socket", return_value=fake_sock):
            ip = runtime_helpers.primary_ip("https://the-company.omnisage.org/api")
        self.assertEqual(ip, "192.168.14.112")
        fake_sock.connect.assert_called_once_with(("the-company.omnisage.org", 80))
        fake_sock.close.assert_called_once()

    def test_falls_back_to_gethostbyname_when_udp_connect_raises(self) -> None:
        """Endpoint host doesn't resolve, or no route. Fallback path
        is ``gethostbyname(gethostname())`` — spec §3.2."""
        fake_sock = MagicMock()
        fake_sock.connect.side_effect = OSError("no route to host")

        with patch.object(socket, "socket", return_value=fake_sock), \
                patch.object(socket, "gethostname", return_value="h"), \
                patch.object(socket, "gethostbyname", return_value="127.0.0.1"):
            ip = runtime_helpers.primary_ip("https://x.example/api")
        self.assertEqual(ip, "127.0.0.1")

    def test_returns_none_when_both_paths_fail(self) -> None:
        fake_sock = MagicMock()
        fake_sock.connect.side_effect = OSError("no route")
        with patch.object(socket, "socket", return_value=fake_sock), \
                patch.object(socket, "gethostname", side_effect=OSError("x")):
            self.assertIsNone(runtime_helpers.primary_ip("https://x.example/api"))

    def test_returns_none_on_empty_endpoint(self) -> None:
        # Empty endpoint short-circuits the UDP branch; fallback also
        # patched away to confirm we don't accidentally succeed.
        with patch.object(socket, "gethostname", side_effect=OSError("x")):
            self.assertIsNone(runtime_helpers.primary_ip(""))


# --------------------------------------------------------------------------- #
# framework_commit()                                                          #
# --------------------------------------------------------------------------- #


class FrameworkCommitTests(unittest.TestCase):
    def test_parses_short_sha_after_plus(self) -> None:
        self.assertEqual(
            runtime_helpers.framework_commit("2026.05.27.01+73bf58d"),
            "73bf58d",
        )

    def test_strips_dirty_suffix(self) -> None:
        self.assertEqual(
            runtime_helpers.framework_commit("2026.05.27.01+73bf58d-dirty"),
            "73bf58d",
        )

    def test_returns_none_when_no_plus_separator(self) -> None:
        self.assertIsNone(runtime_helpers.framework_commit("2026.05.27.01"))

    def test_returns_none_on_empty_or_none(self) -> None:
        self.assertIsNone(runtime_helpers.framework_commit(""))
        self.assertIsNone(runtime_helpers.framework_commit(None))


# --------------------------------------------------------------------------- #
# supervisor_pid()                                                            #
# --------------------------------------------------------------------------- #


class SupervisorPidTests(unittest.TestCase):
    def test_happy_path_reads_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            (inst / "state" / "supervisor").mkdir(parents=True)
            (inst / "state" / "supervisor" / "jc-supervisor.pid").write_text(
                "4242\n", encoding="utf-8"
            )
            self.assertEqual(runtime_helpers.supervisor_pid(inst), 4242)

    def test_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(runtime_helpers.supervisor_pid(Path(tmp)))

    def test_non_integer_contents_return_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst = Path(tmp)
            (inst / "state" / "supervisor").mkdir(parents=True)
            (inst / "state" / "supervisor" / "jc-supervisor.pid").write_text(
                "not-a-pid", encoding="utf-8"
            )
            self.assertIsNone(runtime_helpers.supervisor_pid(inst))


# --------------------------------------------------------------------------- #
# uptime_seconds()                                                            #
# --------------------------------------------------------------------------- #


class UptimeSecondsTests(unittest.TestCase):
    def test_returns_int_delta_from_start_time(self) -> None:
        start = time.time() - 100.7
        self.assertEqual(runtime_helpers.uptime_seconds(start), 100)

    def test_just_started_returns_zero_or_one(self) -> None:
        self.assertIn(runtime_helpers.uptime_seconds(time.time()), (0, 1))


# --------------------------------------------------------------------------- #
# Reporter integration                                                        #
# --------------------------------------------------------------------------- #


def _make_instance(tmp: str) -> Path:
    instance = Path(tmp)
    (instance / "ops").mkdir()
    (instance / "memory" / "L1").mkdir(parents=True)
    (instance / "ops" / "gateway.yaml").write_text(
        "default_brain: claude\n", encoding="utf-8"
    )
    (instance / ".env").write_text(
        "COMPANY_ENDPOINT=http://x\nCOMPANY_API_KEY=key\n", encoding="utf-8"
    )
    return instance


class ReporterSnapshotRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        gw_config.clear_env_cache()

    def test_snapshot_payload_includes_runtime_block(self) -> None:
        """Spec §7 test plan / agent (1).

        Reporter assembles a snapshot. Payload must include a ``runtime``
        dict with the §3.1 keys. Hostname + framework_version come from
        live introspection; primary_ip is patched to a deterministic value
        so the test doesn't depend on the dev box's routing table.
        """
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            # Drop a pid file so supervisor_pid resolves to an int —
            # exercises the happy path through that helper too.
            (instance / "state" / "supervisor").mkdir(parents=True)
            (instance / "state" / "supervisor" / "jc-supervisor.pid").write_text(
                "9999", encoding="utf-8"
            )
            reporter = Reporter(instance)

            with patch("company.runtime.primary_ip", return_value="10.0.0.5"), \
                    patch("company.runtime.hostname", return_value="noah-test"):
                payload = reporter.snapshot()

            self.assertIn("runtime", payload)
            rt = payload["runtime"]
            # All §3.1 keys present (values may be None — checked individually).
            for key in (
                "hostname",
                "primary_ip",
                "framework_version",
                "framework_commit",
                "supervisor_pid",
                "uptime_seconds",
                "reported_at",
            ):
                self.assertIn(key, rt)
            self.assertEqual(rt["hostname"], "noah-test")
            self.assertEqual(rt["primary_ip"], "10.0.0.5")
            self.assertEqual(rt["supervisor_pid"], 9999)
            self.assertIsInstance(rt["uptime_seconds"], int)
            self.assertGreaterEqual(rt["uptime_seconds"], 0)
            # framework_version is whatever conf.framework_version() returns
            # on this checkout — just confirm it's a string and not empty.
            self.assertIsInstance(rt["framework_version"], str)
            self.assertTrue(rt["framework_version"])
            # reported_at is an ISO 8601 stamp.
            self.assertIsInstance(rt["reported_at"], str)
            self.assertIn("T", rt["reported_at"])

    def test_snapshot_survives_hostname_failure(self) -> None:
        """Spec §7 test plan / agent (2).

        Force ``socket.gethostname`` to raise. The snapshot still
        publishes; ``runtime.hostname`` is ``None``; other fields intact.
        """
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            reporter = Reporter(instance)
            with patch.object(socket, "gethostname", side_effect=OSError("nss")):
                payload = reporter.snapshot()
            self.assertIsNone(payload["runtime"]["hostname"])
            # The rest of the snapshot is untouched — pure build_snapshot
            # output keys are still there.
            self.assertIn("queue_depth", payload)
            self.assertIn("framework_version", payload["runtime"])

    def test_snapshot_survives_primary_ip_failure(self) -> None:
        """Spec §7 test plan / agent (3)."""
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            reporter = Reporter(instance)
            with patch("company.runtime.primary_ip", return_value=None):
                payload = reporter.snapshot()
            self.assertIsNone(payload["runtime"]["primary_ip"])
            # No crash: snapshot keys still present.
            self.assertIn("queue_depth", payload)

    def test_uptime_resets_on_new_reporter(self) -> None:
        """Spec §7 test plan / agent (4).

        Two reporters in the same test run = two cold starts. The second
        reporter's uptime must be less than the first's elapsed time at
        the moment we measure — proves ``start_time`` is per-instance,
        not module-level.
        """
        with tempfile.TemporaryDirectory() as tmp:
            instance = _make_instance(tmp)
            r1 = Reporter(instance)
            # Lie about r1 having existed for a while.
            r1.start_time = time.time() - 3600.0
            self.assertGreaterEqual(r1.snapshot()["runtime"]["uptime_seconds"], 3600)

            r2 = Reporter(instance)
            self.assertLess(r2.snapshot()["runtime"]["uptime_seconds"], 5)


if __name__ == "__main__":
    unittest.main()
