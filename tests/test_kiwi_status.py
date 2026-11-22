"""Kiwi /status parsing, per-cycle caching, ADC overloads, noise offset."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

import numpy as np
import pytest

import wspr_recorder.__main__ as m
from wspr_recorder.config import Config, SourceConfig, load_config
from wspr_recorder.kiwi_source import KiwiSourceManager
from wspr_recorder.kiwi_status import fetch_kiwi_status, parse_kiwi_status
from wspr_recorder.noise import overload_delta

FIXTURE = Path(__file__).parent / "fixtures" / "kiwi" / "status.txt"
F20, F30 = 14095600, 10138700
KIWI = "kiwisdr:10.22.23.70:8073"
RADIOD = "radiod:bee1-hf-status.local"


def _mgr(fetcher, clock, freqs=(F20, F30)):
    src = SourceConfig(key=KIWI, status_address="", kind="kiwisdr",
                       host="10.22.23.70", http_port=8073, channels=8)
    return KiwiSourceManager(
        SimpleNamespace(frequencies=list(freqs)), lambda s, c: None, src,
        clock=clock, status_fetcher=fetcher, start_supervisor=False)


def test_parses_adc_ov_from_status_fixture():
    st = parse_kiwi_status(FIXTURE.read_text())
    assert int(st["adc_ov"]) == 0
    assert st["clk_ext_freq"] == "66665900"
    assert st["gps"] == "(-69.0, 90.0)"     # value may contain '=' free text


def test_status_unreachable_returns_none():
    # Port 1 on loopback: connection refused.
    assert fetch_kiwi_status("127.0.0.1", 1, timeout=1.0) is None


def test_ad_over_none_not_zero_when_unreachable():
    mgr = _mgr(lambda h, p: None, lambda: 1_000_020.0)
    assert mgr.read_frontend_ad_over(F20) is None


def test_ad_over_none_when_key_missing_or_garbage():
    t = [1_000_020.0]
    mgr = _mgr(lambda h, p: {"uptime": "5"}, lambda: t[0])
    assert mgr.read_frontend_ad_over(F20) is None
    t[0] += 120
    mgr = _mgr(lambda h, p: {"adc_ov": "x"}, lambda: t[0])
    assert mgr.read_frontend_ad_over(F20) is None


def test_ad_over_shared_by_bands_and_fetched_once_per_cycle():
    calls = []
    t = [1_000_020.0]
    values = iter(["5", "9"])

    def fetcher(host, port):
        calls.append((host, port))
        return {"adc_ov": next(values), "fixes_min": "3"}

    mgr = _mgr(fetcher, lambda: t[0])
    assert mgr.read_frontend_ad_over(F20) == 5
    assert mgr.read_frontend_ad_over(F30) == 5
    assert mgr.get_status()["kiwi_gps_fix"] is True
    assert calls == [("10.22.23.70", 8073)]
    t[0] += 120                       # next cycle -> one more fetch
    assert mgr.read_frontend_ad_over(F20) == 9
    assert mgr.read_frontend_ad_over(F30) == 9
    assert len(calls) == 2


def test_unreachable_polled_once_per_cycle_and_gps_false():
    calls = []
    mgr = _mgr(lambda h, p: calls.append(1), lambda: 1_000_020.0)
    for _ in range(3):
        assert mgr.read_frontend_ad_over(F20) is None
    assert len(calls) == 1
    assert mgr.get_status()["kiwi_gps_fix"] is False


def test_fixture_status_reports_no_gps_fix():
    st = parse_kiwi_status(FIXTURE.read_text())
    mgr = _mgr(lambda h, p: st, lambda: 1_000_020.0)
    mgr.read_frontend_ad_over(F20)
    assert mgr.get_status()["kiwi_gps_fix"] is False


def test_counter_reset_after_kiwi_restart_uses_absolute_value():
    # adc_ov is cumulative since Kiwi boot; the shared delta logic handles
    # a restart (current < last) like the radiod path.
    assert overload_delta(120, 130) == 10
    assert overload_delta(120, 4) == 4
    assert overload_delta(None, 4) == 0


# ---- noise offset ---------------------------------------------------------

def _config_with_offset(extra=""):
    text = (f'[[source]]\nkiwisdr = "10.22.23.70:8073"\nchannels = 8\n{extra}'
            f'[[band]]\nfrequency = {F20}\nmodes = ["W2"]\n')
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "wspr-recorder.toml")
        with open(p, "w") as f:
            f.write(text)
        with mock.patch.object(Config, "validate", return_value=[]):
            return load_config(p)


def test_noise_offset_config_default_and_parse():
    assert _config_with_offset().sources[0].noise_offset_db == 0.0
    s = _config_with_offset("noise_offset_db = -3.5\n").sources[0]
    assert s.noise_offset_db == -3.5


def _noise_row(monkeypatch, tmp_path, rx_source, managers):
    rec = object.__new__(m.WsprRecorder)
    rec.cycle_batcher = MagicMock()
    rec.receiver_managers = managers
    rec._read_cycle_overloads = lambda req, rid: 0
    rec._read_cycle_filter_drops = lambda req, rid: 0
    samples = np.random.RandomState(1).randn(120 * 12000).astype(np.float32) * 0.01
    monkeypatch.setattr(m, "read_wav_mono_float", lambda p: (12000, samples))
    monkeypatch.setattr(m, "wav_format_tag", lambda p: 3)
    req = SimpleNamespace(rx_source=rx_source, band_name="20",
                          frequency_hz=F20)
    rec._submit_noise_for_cycle(req, tmp_path / "x.wav", tmp_path,
                                radiod_id="bee1-hf-status.local",
                                cycle_key=("k",))
    return rec.cycle_batcher.add_noise.call_args.args[2]


def test_noise_offset_applied_to_kiwi_rows_only(monkeypatch, tmp_path):
    def kiwi_mgr(offset):
        src = SourceConfig(key=KIWI, status_address="", kind="kiwisdr",
                           host="10.22.23.70", channels=8,
                           noise_offset_db=offset)
        return KiwiSourceManager(SimpleNamespace(frequencies=[F20]),
                                 lambda s, c: None, src,
                                 start_supervisor=False)

    base = _noise_row(monkeypatch, tmp_path, KIWI, {KIWI: kiwi_mgr(0.0)})
    shifted = _noise_row(monkeypatch, tmp_path, KIWI, {KIWI: kiwi_mgr(2.5)})
    assert base.rms_noise_dbm != 0.0
    assert shifted.rms_noise_dbm == pytest.approx(base.rms_noise_dbm + 2.5)
    # A radiod band is untouched even with a Kiwi manager present.
    radiod = MagicMock()
    r1 = _noise_row(monkeypatch, tmp_path, RADIOD,
                    {RADIOD: radiod, KIWI: kiwi_mgr(2.5)})
    r2 = _noise_row(monkeypatch, tmp_path, RADIOD, {RADIOD: radiod})
    assert r1 == r2
    assert r1.rms_noise_dbm == base.rms_noise_dbm
