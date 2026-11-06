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


def _recorder(active, skipped=True, batcher=True, rate=64_800_000,
              freqs=None, nchan=None):
    from wspr_recorder.receiver_manager import ReceiverManager
    freqs = LOW + HIGH if freqs is None else freqs
    cfg = MagicMock()
    cfg.frequencies = freqs
    rm = ReceiverManager(cfg, lambda *a: None, status_address="x.local",
                         port=5006, source_key=SRC)
    rm._input_samprate_hz = rate
    chan_freqs = LOW if nchan is None else freqs[:nchan]
    for i, f in enumerate(chan_freqs):
        rm.state.channels[i] = SimpleNamespace(
            frequency_hz=f, band_name=freq_to_band_name(f),
            is_active=i < active)
    rm.reprovision_stale = MagicMock()
    rm.full_reset = MagicMock()
    rec = object.__new__(WsprRecorder)
    rec._running = True
    rec.STARTUP_GRACE_PERIOD = 0
    rec.HEALTH_CHECK_INTERVAL = 0
    rec.receiver_managers = {SRC: rm}
    rec.cycle_batcher = None
    if batcher:
        rec.cycle_batcher = CycleBatcher(MagicMock(), deadline_sec=30.0,
                                         backstop_sec=30.0)
    return rec, rm


def _run_checks(rec, rm):
    async def go():
        t = asyncio.create_task(rec._health_check_loop())
        await asyncio.sleep(0.3)
        rec._running = False
        await asyncio.wait_for(t, 2)

    asyncio.run(go())


def test_two_above_nyquist_15_active_is_healthy():
    rec, rm = _recorder(active=15)
    _run_checks(rec, rm)
    rm.reprovision_stale.assert_not_called()
    rm.full_reset.assert_not_called()


def test_real_missing_band_still_degrades():
    rec, rm = _recorder(active=14)
    _run_checks(rec, rm)
    assert rm.reprovision_stale.called or rm.full_reset.called


def test_two_above_nyquist_15_active_is_healthy_without_batcher():
    rec, rm = _recorder(active=15, batcher=False)
    _run_checks(rec, rm)
    rm.reprovision_stale.assert_not_called()
    rm.full_reset.assert_not_called()


def test_band_counts_again_when_rate_rises():
    rec, rm = _recorder(active=15, rate=131_200_000)
    _run_checks(rec, rm)
    assert rm.reprovision_stale.called or rm.full_reset.called


def test_all_bands_above_nyquist_logs_error_once_no_recovery(caplog):
    rec, rm = _recorder(active=0, freqs=list(HIGH), nchan=0)
    with caplog.at_level("ERROR"):
        _run_checks(rec, rm)
    rm.full_reset.assert_not_called()
    rm.reprovision_stale.assert_not_called()
    errs = [r for r in caplog.records
            if "above the radiod Nyquist" in r.getMessage()]
    assert len(errs) == 1


def test_full_reset_relearns_rate():
    rec, rm = _recorder(active=15)
    from wspr_recorder.receiver_manager import ReceiverManager
    rm.connect = MagicMock(return_value=False)
    ReceiverManager.full_reset(rm)
    assert rm._input_samprate_hz is None


def test_rate_reread_prefers_active_channel():
    rec, rm = _recorder(active=1)
    del rm.reprovision_stale  # use the real one
    rm._control = MagicMock()
    rm._read_frontend_samprate = MagicMock(return_value=64_800_000)
    rm._provision = MagicMock(return_value=True)
    rm._verify_and_retry_provisioned = MagicMock()
    rm.state.channels = {7: SimpleNamespace(
        frequency_hz=LOW[0], band_name="x", is_active=False),
        9: SimpleNamespace(frequency_hz=LOW[1], band_name="y",
                           is_active=True)}
    from wspr_recorder.receiver_manager import ReceiverManager
    ReceiverManager.reprovision_stale(rm)
    assert rm._read_frontend_samprate.call_args[0][0] == 9
