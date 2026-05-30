"""Tests for the wall-clock WAV-boundary skew monitor (loss-of-sync).

Each minute boundary, when ``resync_on_skew`` is on, the recorder checks
that it reached ``_samples_per_minute`` samples within
``sync_skew_threshold_sec`` of the grid-predicted UTC minute boundary.
A larger skew means lost samples / sample-clock drift desynced the band
from the WSPR cycle → discard + re-sync.  ``now_fn`` is injected so the
test drives a controlled clock instead of real ``datetime.now()``.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List

import numpy as np

from wspr_recorder.band_recorder import BandRecorder
from wspr_recorder.decode_mode import DecodeMode
from wspr_recorder.sync_strategy import SyncDecision


ANCHOR = datetime(2026, 4, 8, 0, 2, 0, tzinfo=timezone.utc)  # even minute


@dataclass
class MockQuality:
    first_rtp_timestamp: int = 0
    total_samples_delivered: int = 0
    batch_gaps: List = field(default_factory=list)


class FakeSync:
    """Sync strategy that anchors immediately at ``minute_wallclock``."""
    def __init__(self, sample_rate=100, minute_wallclock=ANCHOR):
        self.sample_rate = sample_rate
        self._triggered = False
        self._minute_wallclock = minute_wallclock

    def should_start_minute(self, rtp_ts, packet_samples, wall_clock):
        if self._triggered:
            return None
        self._triggered = True
        return SyncDecision(
            start_wallclock=self._minute_wallclock,
            start_rtp_timestamp=rtp_ts,
            sample_offset=0,
        )

    def on_minute_started(self, rtp_ts, wall_clock):
        pass

    def reset(self):
        self._triggered = False


def feed_minutes(rec, n_minutes, rate, packet_size=20, total_delivered_start=0):
    spm = rate * 60
    total = total_delivered_start
    for _ in range(n_minutes):
        fed = 0
        while fed < spm:
            chunk = min(packet_size, spm - fed)
            total += chunk
            rec.on_samples(
                np.full(chunk, 0.01, dtype=np.float32),
                MockQuality(first_rtp_timestamp=0, total_samples_delivered=total),
            )
            fed += chunk
    return total


class FakeClock:
    """Returns the predicted boundary time + a per-boundary latency.

    The recorder calls this once per minute boundary; call ``k`` (k>=1)
    corresponds to the boundary whose grid-predicted time is
    ``ANCHOR + k*60``.  Pass ``latency`` for a constant offset, or
    ``latencies`` (a list) to script a different skew per boundary
    (e.g. a one-off spike followed by recovery).
    """
    def __init__(self, latency=0.1, latencies=None):
        self.latency = latency
        self.latencies = latencies
        self.n = 0

    def __call__(self):
        self.n += 1
        if self.latencies is not None:
            lat = self.latencies[min(self.n - 1, len(self.latencies) - 1)]
        else:
            lat = self.latency
        return ANCHOR + timedelta(seconds=self.n * 60 + lat)


def _recorder(resync_on_skew, now_fn, rate=100, resync_after=2):
    results = []
    rec = BandRecorder(
        ssrc=1, frequency_hz=14095600, band_name="20",
        sample_rate=rate,
        decode_modes=[DecodeMode.W2],
        on_period_complete=lambda r: results.append(r),
        sync_strategy=FakeSync(sample_rate=rate, minute_wallclock=ANCHOR),
        resync_on_skew=resync_on_skew,
        sync_skew_threshold_sec=0.75,
        sync_resync_after=resync_after,
        now_fn=now_fn,
    )
    return rec, results


def test_in_sync_does_not_resync():
    """Small, constant latency (within threshold) → no re-sync."""
    rec, _results = _recorder(True, FakeClock(latency=0.1))
    feed_minutes(rec, 3, rate=100, packet_size=20)
    assert rec._skew_resyncs == 0
    assert rec._skew_strikes == 0
    assert rec._synced is True
    assert abs(rec._last_skew_sec - 0.1) < 1e-6


def test_sustained_drift_triggers_resync():
    """Two consecutive boundaries beyond threshold trip a re-sync (default
    sync_resync_after=2) and clear sync."""
    rec, results = _recorder(True, FakeClock(latency=2.0))
    n_before = len(results)
    feed_minutes(rec, 2, rate=100, packet_size=20)
    assert rec._skew_resyncs == 1
    # reset() ran: anchor + minute counter cleared, awaiting re-sync.
    assert rec._synced is False
    assert rec._minute_count == 0
    # The re-syncing minute must NOT have emitted a decode request.
    assert len(results) == n_before


def test_lone_strike_does_not_resync_and_emits():
    """A single over-threshold boundary (transient callback delay) must
    NOT discard the cycle — strike recorded, but it emits and recovers."""
    # boundary 1: 2.0s skew (strike); boundary 2: back in tolerance.
    rec, _ = _recorder(True, FakeClock(latencies=[2.0, 0.1, 0.1]))
    feed_minutes(rec, 3, rate=100, packet_size=20)
    assert rec._skew_resyncs == 0       # never crossed the strike count
    assert rec._skew_strikes == 0       # reset by the in-tolerance boundary
    assert rec._synced is True


def test_resync_after_one_when_configured():
    """sync_resync_after=1 trips on the first bad boundary."""
    rec, _ = _recorder(True, FakeClock(latency=2.0), resync_after=1)
    feed_minutes(rec, 1, rate=100, packet_size=20)
    assert rec._skew_resyncs == 1
    assert rec._synced is False


def test_negative_skew_also_trips():
    """Samples arriving FASTER than real time (now before boundary) trips
    too — abs(skew) is what matters."""
    rec, _ = _recorder(True, FakeClock(latency=-3.0), resync_after=1)
    feed_minutes(rec, 1, rate=100, packet_size=20)
    assert rec._skew_resyncs == 1
    assert rec._synced is False


def test_monitor_off_by_default_never_resyncs():
    """With the monitor off (default), a wildly wrong clock is ignored —
    this is what keeps the synthetic-clock suite from tripping."""
    # Default now_fn = real datetime.now(); ANCHOR is years in the past,
    # so skew would be enormous IF the monitor were on.
    rec, _ = _recorder(False, None)
    feed_minutes(rec, 2, rate=100, packet_size=20)
    assert rec._skew_resyncs == 0
    assert rec._synced is True


def test_resync_recovers_on_next_clean_boundary():
    """After a drift trip, a subsequent in-sync stretch re-anchors and
    runs normally (no further re-syncs)."""
    clock = FakeClock(latency=2.0)
    rec, _ = _recorder(True, clock, resync_after=1)
    feed_minutes(rec, 1, rate=100, packet_size=20)   # trips, resets
    assert rec._skew_resyncs == 1
    assert rec._synced is False
    # Now feed clean minutes with in-tolerance latency.  FakeSync.reset()
    # ran inside recorder.reset(), so it re-anchors at ANCHOR; swap in a
    # healthy clock for the post-resync boundaries.
    clock.latency = 0.1
    clock.n = 0
    feed_minutes(rec, 2, rate=100, packet_size=20)
    assert rec._synced is True
    assert rec._skew_resyncs == 1            # no new trips
    assert abs(rec._last_skew_sec - 0.1) < 1e-6
