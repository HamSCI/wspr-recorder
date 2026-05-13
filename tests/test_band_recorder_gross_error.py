"""Tests for BandRecorder's gross-error tripwire.

The existing CLOCK ERROR log at >=1s is preserved as an early warning.
The new behavior: after GROSS_TRIPS_FOR_EXIT consecutive minute boundaries
with |drift| >= GROSS_DRIFT_SEC, BandRecorder calls sys.exit() so systemd
restarts the recorder with a fresh anchor (matching wsprdaemon-client's
existing `Restart=always` policy).

We don't drive a full minute of samples here — we exercise the boundary
handler directly with controlled wallclock values, mocking
`datetime.now(timezone.utc)` to inject drift between
`minute_wallclock` (grid-propagated from the original anchor) and
`actual_wallclock` (mocked "now").
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

import numpy as np

from wspr_recorder.band_recorder import (
    GROSS_DRIFT_SEC,
    GROSS_EXIT_CODE,
    GROSS_TRIPS_FOR_EXIT,
    BandRecorder,
)
from wspr_recorder.decode_mode import DecodeMode
from wspr_recorder.sync_strategy import SyncDecision


class _ImmediateSync:
    """Trigger sync on the very first batch we see."""
    def __init__(self, sample_rate, anchor_wallclock):
        self.sample_rate = sample_rate
        self.samples_per_minute = sample_rate * 60
        self._fired = False
        self._anchor = anchor_wallclock

    def should_start_minute(self, rtp_ts, _samples, _now):
        if self._fired:
            return None
        self._fired = True
        return SyncDecision(
            start_wallclock=self._anchor,
            start_rtp_timestamp=rtp_ts,
            sample_offset=0,
        )

    def on_minute_started(self, rtp_ts, wallclock):
        pass


def _make_recorder():
    """Build a BandRecorder ready to call _on_minute_boundary() directly.

    The recorder has its _first_wallclock anchor wired to 2026-05-13T19:00:00,
    so for minute_count=N, the projected minute_wallclock is anchor + N*60s.
    """
    rec = BandRecorder(
        ssrc=1,
        frequency_hz=14_095_600,
        band_name="20",
        sample_rate=1200,
        decode_modes=[DecodeMode.W2],
        on_period_complete=lambda r: None,
        sync_strategy=_ImmediateSync(
            sample_rate=1200,
            anchor_wallclock=datetime(2026, 5, 13, 19, 0, 0,
                                       tzinfo=timezone.utc),
        ),
    )
    # Manually wire the post-sync state so _on_minute_boundary can run.
    rec._synced = True
    rec._first_wallclock = datetime(2026, 5, 13, 19, 0, 0,
                                     tzinfo=timezone.utc)
    rec._first_rtp_timestamp = 0
    rec._minute_count = 0
    return rec


def _trigger_minute_with_drift(rec, drift_seconds: float) -> None:
    """Call _on_minute_boundary while pretending wall clock = anchor + N*60 +
    drift.  The DriftTracker will see (actual - expected) = drift_seconds."""
    rec._minute_count_next = rec._minute_count + 1  # informational
    # Mock datetime.now to inject a controlled drift.  _on_minute_boundary
    # reads datetime.now(timezone.utc) once and feeds it to the tracker.
    expected_wallclock = rec._first_wallclock + (
        (rec._minute_count + 1) * (rec.sample_rate * 60) / rec.sample_rate
    ) * _seconds_to_timedelta_unit()                # placeholder; computed below
    # Simpler: drift_seconds is what we want delta_ms to come out to,
    # so we override the tracker directly to inject the value.  Cleaner
    # than fighting datetime.now's call site.
    with mock.patch.object(rec._drift_tracker, "observe") as obs:
        obs.return_value = mock.MagicMock(
            delta_ms=drift_seconds * 1000.0,
            cumulative_drift_ms=drift_seconds * 1000.0,
        )
        # _on_minute_boundary will also try to extract from the ring.
        # That'll be a no-op because the ring is empty.  Patch _ring.close_minute
        # and _ring.minutes_available so we don't blow up there.
        with mock.patch.object(rec._ring, "close_minute"):
            with mock.patch.object(type(rec._ring), "minutes_available",
                                    new_callable=mock.PropertyMock,
                                    return_value=0):
                rec._on_minute_boundary()


def _seconds_to_timedelta_unit():
    """Unused helper retained to keep IDE happy; the test patches drift
    directly so this isn't called."""
    from datetime import timedelta
    return timedelta(seconds=1)


class TestGrossErrorTripwire(unittest.TestCase):

    def test_subthreshold_drift_does_not_increment_counter(self):
        """Drift below GROSS_DRIFT_SEC must not increment the trip counter."""
        rec = _make_recorder()
        # 1.5 s drift: above the 1s log threshold (expected to log) but
        # below the 2s exit threshold.
        for _ in range(5):
            _trigger_minute_with_drift(rec, GROSS_DRIFT_SEC - 0.5)
        self.assertEqual(rec._gross_trips, 0)

    def test_single_gross_trip_logs_but_does_not_exit(self):
        """One bad minute is not enough to exit — could be transient."""
        rec = _make_recorder()
        _trigger_minute_with_drift(rec, GROSS_DRIFT_SEC + 0.5)
        self.assertEqual(rec._gross_trips, 1)
        # Not yet at the exit threshold (which is 2 by default).
        self.assertLess(rec._gross_trips, GROSS_TRIPS_FOR_EXIT)

    def test_consecutive_gross_trips_trigger_sys_exit(self):
        """K consecutive bad minutes → sys.exit(GROSS_EXIT_CODE)."""
        rec = _make_recorder()
        with self.assertRaises(SystemExit) as cm:
            for _ in range(GROSS_TRIPS_FOR_EXIT):
                _trigger_minute_with_drift(rec, GROSS_DRIFT_SEC + 0.5)
        self.assertEqual(cm.exception.code, GROSS_EXIT_CODE)

    def test_clean_minute_resets_trip_counter(self):
        """A single bad minute followed by a clean one must reset the
        counter — otherwise transient hiccups would slowly accumulate to
        a false exit over hours."""
        rec = _make_recorder()
        # One trip.
        _trigger_minute_with_drift(rec, GROSS_DRIFT_SEC + 0.5)
        self.assertEqual(rec._gross_trips, 1)
        # Clean minute.
        _trigger_minute_with_drift(rec, 0.0)
        self.assertEqual(rec._gross_trips, 0)


if __name__ == "__main__":
    unittest.main()
