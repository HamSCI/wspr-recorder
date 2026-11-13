"""Tests for wspr-recorder's in-process hs-uploader shim.

v3 Phase A absorbed the previously-standalone ``wd-upload-hs@.service``
from wsprdaemon-client into wspr-recorder.  These tests are the
direct port of wsprdaemon-client/tests/test_hs_uploader_shim.py
adapted to the new package location.

Focused on the bits the shim itself owns:
  - from_env: env-var → constructor argument plumbing
  - start() gating on WSPR_USE_HS_UPLOADER feature flag
  - pipeline construction wiring (which transports + sources end up
    in the Uploader, given which env signals)
  - routing: no direct wsprnet.org pipeline; the wsprdaemon tar
    carries the forward flags instead
  - the per-cycle journal line

Transport / source internals (SFTP, HTTP POST, FileTreeSource
delete-on-ack) are covered in hs-uploader's own test suite; we
don't re-test them here.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _hs_available() -> bool:
    try:
        import hs_uploader  # noqa: F401
        return True
    except ImportError:
        return False


from wspr_recorder.hs_uploader_shim import (
    WsprUploaderHs,
    _server_host,
)


# ---- env → constructor --------------------------------------------------


class TestFromEnv(unittest.TestCase):
    BASE_ENV = {
        "WD_RECEIVER_CALL": "AC0G/B1",
        "WD_RECEIVER_GRID": "EM38ww",
        "WD_UPLOAD_WSPRDAEMON_DIR": "/var/spool/wsprdaemon/posting/uploads/wsprdaemon/AC0G=B1",
        "WD_UPLOAD_WSPRNET_DIR": "/var/spool/wsprdaemon/posting/uploads/wsprnet/AC0G=B1_EM38ww",
        "WD_SFTP_SERVERS": "wsprdaemon@gw1.wsprdaemon.org,wsprdaemon@gw2.wsprdaemon.org",
        "SIGMOND_SQLITE_PATH": "/var/lib/sigmond/sink.db",
    }

    def test_required_fields_parse(self):
        u = WsprUploaderHs.from_env(self.BASE_ENV)
        self.assertEqual(u._call, "AC0G/B1")
        self.assertEqual(u._grid, "EM38ww")
        self.assertEqual(
            u._sftp_servers,
            ["wsprdaemon@gw1.wsprdaemon.org", "wsprdaemon@gw2.wsprdaemon.org"],
        )

    def test_missing_call_raises(self):
        env = dict(self.BASE_ENV)
        del env["WD_RECEIVER_CALL"]
        with self.assertRaises(ValueError):
            WsprUploaderHs.from_env(env)

    def test_missing_grid_raises(self):
        env = dict(self.BASE_ENV)
        del env["WD_RECEIVER_GRID"]
        with self.assertRaises(ValueError):
            WsprUploaderHs.from_env(env)

    def test_legacy_single_sftp_server_falls_back(self):
        env = dict(self.BASE_ENV)
        del env["WD_SFTP_SERVERS"]
        env["WD_SFTP_SERVER"] = "wsprdaemon@gw.wsprdaemon.org"
        u = WsprUploaderHs.from_env(env)
        self.assertEqual(u._sftp_servers, ["wsprdaemon@gw.wsprdaemon.org"])

    def test_optional_dirs_can_be_absent(self):
        env = {"WD_RECEIVER_CALL": "AC0G/B1", "WD_RECEIVER_GRID": "EM38ww"}
        u = WsprUploaderHs.from_env(env)
        self.assertIsNone(u._wsprdaemon_dir)
        self.assertEqual(u._sftp_servers, [])

    def test_instance_name_default_is_call_with_slash_replaced(self):
        u = WsprUploaderHs.from_env(self.BASE_ENV)
        self.assertEqual(u._instance_name, "AC0G_B1")


# ---- WSPR_USE_HS_UPLOADER feature flag ----------------------------------


class TestFeatureFlag(unittest.TestCase):
    def _shim(self):
        return WsprUploaderHs.from_env({
            "WD_RECEIVER_CALL": "AC0G", "WD_RECEIVER_GRID": "EM38ww",
        })

    def test_flag_unset_no_thread(self):
        u = self._shim()
        with patch.dict(os.environ, {"WSPR_USE_HS_UPLOADER": ""}, clear=False):
            u.start()
        self.assertFalse(u.is_active)
        self.assertIsNone(u._thread)


# ---- direct wake() path (replaces SIGUSR1+pidfile) -------------------


class TestWakeMethod(unittest.TestCase):
    """``wake()`` is now the canonical path the in-process producer
    (CycleBatcher) uses to nudge the pump.  The legacy SIGUSR1+pidfile
    dance was retired post-Phase-A; verify the direct-call path works
    and that the pid file is no longer written/cleaned-up."""

    def _shim(self):
        return WsprUploaderHs.from_env({
            "WD_RECEIVER_CALL": "AC0G", "WD_RECEIVER_GRID": "EM38ww",
        })

    def test_wake_is_safe_before_start(self):
        """Construction-time wake() (e.g. a callback fires before
        start() has happened) is a clean no-op."""
        u = self._shim()
        u.wake()                                # no-op, no AttributeError
        self.assertFalse(hasattr(u, "_wake"))

    def test_wake_sets_event_after_start(self):
        """Once start() has constructed the Event, wake() sets it."""
        u = self._shim()
        # Bypass real start() — we don't need real pipelines, just the
        # _wake Event the pump loop would block on.
        import threading
        u._wake = threading.Event()
        u.wake()
        self.assertTrue(u._wake.is_set())

    def test_shim_does_not_write_pid_file(self):
        """Post-Phase-A retirement: the shim must NOT create the
        legacy /run/wsprdaemon/wd-upload-hs.pid file.  Guards against
        a regression that would put the pidfile back."""
        self.assertFalse(
            hasattr(WsprUploaderHs, "_pid_file_path"),
            "pid file path helper should have been removed post-Phase-A",
        )


# ---- pipeline wiring ----------------------------------------------------


@unittest.skipUnless(_hs_available(), "hs-uploader not importable")
class TestPipelineWiring(unittest.TestCase):
    """Verify which pipelines get built under which env signals.
    We don't actually start the pump thread — the pipelines are
    inspected on `_uploader.pipelines` after start() returns.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # The shim constructs hs_uploader's watermark store, whose
        # default_path() falls back to /var/lib/hs-uploader.  Unset, these
        # tests reach for that real system path: they pass on a station,
        # where it exists, and fail everywhere else -- passing for a reason
        # unrelated to the pipeline wiring they exist to check.
        self._prev_state_dir = os.environ.get("HS_UPLOADER_STATE_DIR")
        os.environ["HS_UPLOADER_STATE_DIR"] = self.tmp
        self.sink = Path(self.tmp) / "sink.db"
        # Materialise an empty pending_uploads schema so SqliteSource's
        # _ensure_ready() finds it.
        import sqlite3
        conn = sqlite3.connect(self.sink)
        conn.execute(
            "CREATE TABLE pending_uploads ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "target_db TEXT, target_table TEXT, "
            "schema_version INTEGER, payload_json TEXT, queued_at TEXT)"
        )
        conn.commit()
        conn.close()
        self.wsprd_dir = Path(self.tmp) / "wsprdaemon_spool"
        self.wsprd_dir.mkdir()
        self.wsprn_dir = Path(self.tmp) / "wsprnet_spool"
        self.wsprn_dir.mkdir()

    def tearDown(self):
        import shutil
        if self._prev_state_dir is None:
            os.environ.pop("HS_UPLOADER_STATE_DIR", None)
        else:
            os.environ["HS_UPLOADER_STATE_DIR"] = self._prev_state_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self, **overrides):
        env = {
            "WD_RECEIVER_CALL": "AC0G/B1",
            "WD_RECEIVER_GRID": "EM38ww",
            "WD_UPLOAD_WSPRDAEMON_DIR": str(self.wsprd_dir),
            "WD_UPLOAD_WSPRNET_DIR": str(self.wsprn_dir),
            "WD_SFTP_SERVERS": "wsprdaemon@gw.wsprdaemon.org",
            "SIGMOND_SQLITE_PATH": str(self.sink),
        }
        env.update(overrides)
        u = WsprUploaderHs.from_env(env)
        # Set just-this-call SIGMOND_SQLITE_PATH so SqliteSource.from_env
        # in the shim sees it.
        with patch.dict(os.environ, {
            "WSPR_USE_HS_UPLOADER": "1",
            "SIGMOND_SQLITE_PATH": str(self.sink),
        }, clear=False):
            u.start()
        return u

    def _wsprd_pipe(self, u):
        return next(
            (p for p in u._uploader.pipelines
             if p.name.startswith("wsprdaemon-tar")),
            None,
        )

    def test_full_env_builds_no_wsprnet_pipeline(self):
        """Uniform routing: every WSPR spot goes only to wsprdaemon.org,
        which forwards a copy to wsprnet.org.  The shim must build no
        pipeline and no transport that posts to wsprnet.org itself."""
        u = self._start()
        try:
            self.assertTrue(u.is_active)
            names = [p.name for p in u._uploader.pipelines]
            self.assertTrue(any(n.startswith("wsprdaemon-tar") for n in names))
            self.assertFalse(
                any("wsprnet" in n for n in names),
                f"no wsprnet pipeline expected, got {names}",
            )
            from hs_uploader.transports.wsprnet import WsprNet
            transports = [p.transport for p in u._uploader.pipelines]
            self.assertFalse(
                any(isinstance(t, WsprNet) for t in transports),
                "the shim must construct no WsprNet transport",
            )
        finally:
            u.stop(timeout=2.0)

    def test_no_env_knob_brings_direct_posting_back(self):
        """The old wsprnet knobs no longer reach a WsprNet transport."""
        u = self._start(
            WSPRNET_API_BASE="legacy",
            WSPRNET_BATCH_SIZE="1",
            WSPRNET_POLL_INTERVAL_SEC="5",
            WSPRNET_AUDIT="1",
        )
        try:
            names = [p.name for p in u._uploader.pipelines]
            self.assertEqual(
                [n for n in names if "wsprnet" in n], [],
            )
        finally:
            u.stop(timeout=2.0)

    def test_wsprdaemon_tar_carries_forward_flag_and_no_identity(self):
        """The tar transport asks the server to forward to wsprnet.org
        and names no reporter: the server posts under each spot's own
        rx_sign (sigmond uploader_manifest.apply_proxying semantics)."""
        u = self._start()
        try:
            pipe = self._wsprd_pipe(u)
            self.assertIsNotNone(pipe)
            t = pipe.transport
            self.assertIs(t.forward_to_wsprnet, True)
            self.assertIsNone(t.wsprnet_reporter)
            self.assertIsNone(t.psk_reporter)
            # The FTP fallback rebuilds the tar itself, so it must carry
            # the same flag, or a cycle shipped by FTP would not forward.
            ftp = t.fallback_ftp
            self.assertIsNotNone(ftp)
            self.assertIs(ftp.forward_to_wsprnet, True)
            self.assertIsNone(ftp.wsprnet_reporter)
            self.assertIsNone(ftp.psk_reporter)
        finally:
            u.stop(timeout=2.0)

    def test_built_tar_routing_json_forwards_wsprnet_and_psk(self):
        """End to end through the real tar builder: routing.json flags
        forward_to_wsprnet and forward_to_pskreporter, with no reporter
        identity, for a cycle that carries an FT8 row."""
        import io
        import json
        import tarfile
        from datetime import datetime, timezone
        from hs_uploader import StationIdentity
        from hs_uploader.core import Record, RecordBatch
        u = self._start()
        try:
            t = self._wsprd_pipe(u).transport
            when = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
            rec = Record(
                table="psk.spots", time=when,
                columns={
                    "time": "2026-11-13T12:00:00Z", "mode": "FT8",
                    "frequency": 14074000, "snr_db": -10, "dt": 0.1,
                    "tx_call": "K1ABC", "grid": "FN42",
                    "host_call": "AC0G/B1", "host_grid": "EM38ww",
                    "radiod_id": "rx1",
                },
            )
            ident = StationIdentity.load()
            ident.call, ident.grid = "AC0G/B1", "EM38ww"
            raw = t._build_tar(RecordBatch(records=[rec], cursor_after=b""),
                               ident)
            self.assertIsNotNone(raw)
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
                routing = json.loads(tf.extractfile("routing.json").read())
            default = routing["default"]
            self.assertIs(default.get("forward_to_wsprnet"), True)
            self.assertIs(default.get("forward_to_pskreporter"), True)
            self.assertNotIn("wsprnet_reporter", default)
            self.assertNotIn("psk_reporter", default)
        finally:
            u.stop(timeout=2.0)

    def test_wsprdaemon_pipeline_skipped_when_no_sftp(self):
        """No SFTP target leaves nothing to build: the shim has no
        direct wsprnet pipeline to fall back on."""
        u = self._start(WD_SFTP_SERVERS="")
        try:
            names = [p.name for p in (u._uploader.pipelines if u._uploader else [])]
            self.assertEqual(names, [])
            self.assertFalse(u.is_active)
        finally:
            u.stop(timeout=2.0)

    def test_psk_rides_in_wsprdaemon_cycle_tar_not_a_separate_pipeline(self):
        """PSK (FT8/FT4) ships inside the per-cycle wsprdaemon tar via
        ``WsprCycleSource(include_psk=True)`` — there is NO separate
        psk-tar pipeline.  The multi-rx tags
        that Phase D Cut 4 pins (``rx_source`` + ``frequency_bucket_hz``,
        stamped on every psk spot by psk-recorder in Phase A / Cut 2)
        reach wsprdaemon-server because WsprCycleSource ships each
        psk.spots row's WHOLE ``payload_json`` — no column allow-list to
        forget.  (That passthrough is covered directly in hs-uploader's
        own WsprCycleSource suite.)"""
        u = self._start()
        try:
            names = [p.name for p in u._uploader.pipelines]
            # No standalone psk-tar pipeline is constructed.
            self.assertFalse(
                any(n.startswith("psk-tar") for n in names),
                "PSK now rides in the wsprdaemon-tar; there should be no "
                "separate psk-tar pipeline",
            )
            # The wsprdaemon-tar pipeline's source opts PSK in.
            wsprd_pipe = next(
                (p for p in u._uploader.pipelines
                 if p.name.startswith("wsprdaemon-tar")),
                None,
            )
            self.assertIsNotNone(
                wsprd_pipe,
                "wsprdaemon-tar pipeline should be constructed with a "
                "complete env",
            )
            self.assertTrue(
                getattr(wsprd_pipe.source, "include_psk", False),
                "WsprCycleSource must be built with include_psk=True so "
                "psk.spots ride in the per-cycle tar",
            )
        finally:
            u.stop(timeout=2.0)


# ---- per-cycle journal line ------------------------------------------


class TestCycleLogLine(unittest.TestCase):
    """The per-cycle line reports wsprnet=0: the shim posts nothing to
    wsprnet.org; wsprdaemon.org forwards the spots.  ``smd watch
    uploads`` parses the line with a regex that needs the field."""

    SMD_RE = (
        r"wspr-uploader-hs:\s+cycle=(\S+)\s+shipped\s+"
        r"wsprdaemon=(\d+)\s+wsprnet=(\S+)\s+ft8=(\d+)\s+ft4=(\d+)\s+tar=(\S+)"
    )

    def _pump_once(self, u, pipeline_names):
        import re
        import threading
        from datetime import datetime, timezone
        from types import SimpleNamespace as NS
        when = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
        rec = NS(table="wspr.spots", time=when, columns={})
        outcome = NS(kind="acked", reason="1/1 added", n_bytes=2048)

        class FakeUploader:
            def pump(inner):
                for name in pipeline_names:
                    u._on_batch_outcome(
                        NS(name=name), NS(records=[rec, rec]), outcome,
                    )
                u._stop.set()
                return True

        u._uploader = FakeUploader()
        u._wake = threading.Event()
        u._wake.set()
        with self.assertLogs("wspr_recorder.hs_uploader_shim", "INFO") as cm:
            u._run()
        lines = [m for m in cm.output if "shipped" in m]
        self.assertEqual(len(lines), 1, cm.output)
        m = re.search(self.SMD_RE, lines[0])
        self.assertIsNotNone(m, lines[0])
        return m

    def _shim(self):
        return WsprUploaderHs.from_env({
            "WD_RECEIVER_CALL": "AC0G", "WD_RECEIVER_GRID": "EM38ww",
        })

    def test_cycle_line_reports_wsprnet_zero(self):
        m = self._pump_once(self._shim(), ["wsprdaemon-tar-AC0G"])
        self.assertEqual(m.group(2), "2")
        self.assertEqual(m.group(3), "0")

    def test_wsprnet_named_outcome_is_not_counted(self):
        """Even an outcome from a pipeline named wsprnet-* cannot raise
        the count: the shim has no wsprnet tally left to raise."""
        m = self._pump_once(
            self._shim(), ["wsprdaemon-tar-AC0G", "wsprnet-AC0G"],
        )
        self.assertEqual(m.group(3), "0")


# ---- _server_host utility ----------------------------------------------


class TestServerHost(unittest.TestCase):
    def test_strips_user_prefix(self):
        self.assertEqual(
            _server_host("wsprdaemon@gw1.wsprdaemon.org"),
            "gw1.wsprdaemon.org",
        )

    def test_passes_through_when_no_user(self):
        self.assertEqual(
            _server_host("gw1.wsprdaemon.org"),
            "gw1.wsprdaemon.org",
        )


if __name__ == "__main__":
    unittest.main()
