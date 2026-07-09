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
