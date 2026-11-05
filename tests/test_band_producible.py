"""Nyquist producibility guard: a band above the radiod ADC Nyquist can't be produced.

Ported from wsprdaemon 3.4.0 (which reads [rx888] samprate from the radiod conf);
here the front-end rate is learned over the wire via poll_status().frontend.input_samprate.
"""
from __future__ import annotations

from wspr_recorder.receiver_manager import _band_producible

# wsprdaemon dial frequencies (Hz)
_F_40M = 7_038_600
_F_10M = 28_124_600
_F_8M = 40_680_000
_F_6M = 50_293_000


def test_all_bands_producible_at_129_6_msps():
    sr = 129_600_000  # Nyquist 64.8 MHz — every WSPR/FT band fits
    for f in (_F_40M, _F_10M, _F_8M, _F_6M):
        assert _band_producible(f, sr) is True


def test_8m_6m_not_producible_at_64_8_msps():
    sr = 64_800_000  # Nyquist 32.4 MHz — 8m/6m are above it
    assert _band_producible(_F_40M, sr) is True
    assert _band_producible(_F_10M, sr) is True
    assert _band_producible(_F_8M, sr) is False
    assert _band_producible(_F_6M, sr) is False


def test_fail_open_when_front_end_rate_unknown():
    # Unknown/zero rate must never skip a band (guard fails open).
    for sr in (None, 0):
        for f in (_F_40M, _F_10M, _F_8M, _F_6M):
            assert _band_producible(f, sr) is True


def test_reprovision_quiet_and_callback_for_above_nyquist():
    """Skipped band reports unproducible to the callback; reprovision does
    not retry it while the front-end rate is unchanged."""
    from unittest.mock import MagicMock
    from wspr_recorder.receiver_manager import ReceiverManager

    cfg = MagicMock()
    cfg.frequencies = [_F_40M, _F_8M]
    events = []
    rm = ReceiverManager(cfg, lambda *a: None, status_address="x.local",
                         port=5006, source_key="radiod:x",
                         on_band_producible=lambda s, b, ok: events.append((s, b, ok)))
    rm._control = MagicMock()
    rm._input_samprate_hz = 64_800_000
    assert rm._provision(_F_8M) is False
    assert events == [("radiod:x", "8", False)]
    rm._provision = MagicMock(return_value=True)
    rm._read_frontend_samprate = MagicMock(return_value=64_800_000)
    rm._verify_and_retry_provisioned = MagicMock()
    # 40 m has no channel, 8 m is above Nyquist: only 40 m is retried.
    rm.reprovision_stale()
    rm._provision.assert_called_once_with(_F_40M)
