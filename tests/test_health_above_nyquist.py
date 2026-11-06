"""Channel-health check counts only bands radiod can produce."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from wspr_recorder.__main__ import WsprRecorder
from wspr_recorder.config import freq_to_band_name
from wspr_recorder.spot_sink import CycleBatcher

SRC = "radiod:A"
# 15 producible HF bands plus the two above 32.4 MHz Nyquist (6 m, 8 m).
LOW = [1_836_600 + i * 100_000 for i in range(15)]
HIGH = [50_293_000, 40_680_000]


def _recorder(active, skipped):
    freqs = LOW + HIGH
    chans = {}
    for i, f in enumerate(LOW):
        chans[i] = SimpleNamespace(
            frequency_hz=f, band_name=freq_to_band_name(f),
            is_active=i < active)
    rm = MagicMock()
    rm.config.frequencies = freqs
    rm.state.channels = chans
    rec = object.__new__(WsprRecorder)
    rec._running = True
    rec.STARTUP_GRACE_PERIOD = 0
    rec.HEALTH_CHECK_INTERVAL = 0
    rec.receiver_managers = {SRC: rm}
    rec.cycle_batcher = CycleBatcher(MagicMock(), deadline_sec=30.0,
                                     backstop_sec=30.0)
    for f in skipped:
        rec.cycle_batcher.set_band_producible(
            SRC, freq_to_band_name(f), False)
    return rec, rm


def _run_checks(rec, rm):
    async def go():
        t = asyncio.create_task(rec._health_check_loop())
        await asyncio.sleep(0.3)
        rec._running = False
        await asyncio.wait_for(t, 2)

    asyncio.run(go())


def test_two_above_nyquist_15_active_is_healthy():
    rec, rm = _recorder(active=15, skipped=HIGH)
    _run_checks(rec, rm)
    rm.reprovision_stale.assert_not_called()
    rm.full_reset.assert_not_called()


def test_real_missing_band_still_degrades():
    rec, rm = _recorder(active=14, skipped=HIGH)
    _run_checks(rec, rm)
    assert rm.reprovision_stale.called or rm.full_reset.called


def test_band_counts_again_when_producible():
    rec, rm = _recorder(active=15, skipped=HIGH)
    for f in HIGH:
        rec.cycle_batcher.set_band_producible(
            SRC, freq_to_band_name(f), True)
    _run_checks(rec, rm)
    assert rm.reprovision_stale.called or rm.full_reset.called
