"""Sub-Hz spot frequency end to end, and the FST4W spread (G3ZIL, 2026-11-23).

1. spot_to_row keeps the decoder's frequency at full resolution
   (``frequency_mhz_full``) beside the integer-Hz dedupe key, and
   hs-uploader's wsprdaemon line carries it to 9 decimals.
2. FST4W's metric is jt9's w50 (spread at 50 %, Hz), not dmin.
3. wsprd gets ``-F`` and jt9 ``--fine-freq`` when their help lists them.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

from wspr_recorder import decoder as decoder_mod
from wspr_recorder.callsign_db import CallsignDB
from wspr_recorder.decoder import DecoderRunner, RawSpot
from wspr_recorder.spot_sink import SCHEMA_VERSION, spot_to_row


def _runner(**kw) -> DecoderRunner:
    return DecoderRunner(
        band_name="20", frequency_hz=14_095_600,
        work_dir=Path(tempfile.mkdtemp()), callsign_db=CallsignDB(), **kw,
    )


def _spot(freq: float) -> RawSpot:
    return RawSpot(date="261123", time="1200", snr=-20, dt=0.3, freq=freq,
                   call="G3ZIL", grid="IO90", power=23, pkt_mode=2)


# ---- 1. the sink row ------------------------------------------------------

def test_row_keeps_full_resolution_frequency():
    row = spot_to_row(_spot(14.097123456), band="20", radiod_id="rx",
                      rx_call="AC0G", rx_grid="EM38ww", host_id="h")
    assert row["frequency_mhz_full"] == 14.097123456
    assert row["frequency_hz"] == 14_097_123          # dedupe key unchanged


def test_full_frequency_survives_payload_json_and_the_wsprdaemon_line(
        tmp_path, monkeypatch):
    """spot_sink row -> hs_uploader Writer (sink.db payload_json) ->
    wsprdaemon transport's extended line: 9 decimals intact."""
    from hs_uploader.sink import Writer
    from hs_uploader.transports.wsprdaemon import _format_extended_line

    monkeypatch.setenv("SIGMOND_SQLITE_PATH", str(tmp_path / "sink.db"))
    writer = Writer.from_env(mode="wspr", table="spots",
                             schema_version=SCHEMA_VERSION)
    row = spot_to_row(_spot(14.097123456), band="20", radiod_id="rx",
                      rx_call="AC0G", rx_grid="EM38ww", host_id="h")
    writer.insert([row])
    writer.flush()
    writer.close()
    con = sqlite3.connect(tmp_path / "sink.db")
    (payload,) = con.execute(
        "SELECT payload_json FROM pending_uploads").fetchone()
    cols = json.loads(payload)
    line = _format_extended_line(cols, rx_call="AC0G", rx_grid="EM38ww")
    assert line.split()[5] == "14.097123456"


def test_old_row_without_full_frequency_still_renders():
    from hs_uploader.transports.wsprdaemon import _format_extended_line
    row = spot_to_row(_spot(14.097123456), band="20", radiod_id="rx",
                      rx_call="AC0G", rx_grid="EM38ww", host_id="h")
    del row["frequency_mhz_full"]                       # a pre-fix row
    line = _format_extended_line(row, rx_call="AC0G", rx_grid="EM38ww")
    assert line.split()[5] == "14.097123000"


# ---- 2. FST4W metric is w50 -------------------------------------------------

# Verbatim jt9 fst4_decodes.dat line (KJ6MKI 40 m, 2026-10-28, WB6CXC):
# nutc icand itry nsyncoh iaptype ijitter npct ntype Keff nsync_qual
# nharderrors dmin nhp hd sync xsnr xdt fsig w50 msg
REAL_FST4 = ("000526   2  1  8  0  0  0  1  66  80   0   0.0   0   0.0"
             "    43.16 -10.3  0.56 1535.0  0.012 WB6CXC CM88 30")
REAL_FST4_SPREAD = ("000526   5  1  8  0  0  0  1  66  74  12   3.5  12   3.5"
                    "     7.63 -18.6  0.48 1460.0  0.258 <2288505> EM79 30")


def test_fst4_metric_is_w50_not_dmin():
    spot = _runner()._parse_fst4_line(REAL_FST4)
    assert spot.metric == pytest.approx(0.012)
    spot = _runner()._parse_fst4_line(REAL_FST4_SPREAD)
    assert spot.metric == pytest.approx(0.258)          # dmin there is 3.5


def test_fst4_w50_reaches_the_row_as_millihertz():
    from hs_uploader.transports.wsprdaemon import _format_extended_line
    spot = _runner()._parse_fst4_line(REAL_FST4_SPREAD)
    spot.date, spot.time, spot.pkt_mode = "261028", "0526", 3
    row = spot_to_row(spot, band="20", radiod_id="rx", rx_call="AC0G",
                      rx_grid="EM38ww", host_id="h")
    line = _format_extended_line(row, rx_call="AC0G", rx_grid="EM38ww")
    assert int(line.split()[13]) == 258                 # w50 in mHz


def test_fst4_fine_freq_line_parses_to_the_millihertz():
    """``jt9 --fine-freq`` writes fsig as f9.3 in f7.1's place; nothing
    else moves (ubersdr 262b4db decodes_dat_line)."""
    fine = REAL_FST4.replace(" 1535.0 ", " 1535.123 ")
    spot = _runner()._parse_fst4_line(fine)
    assert spot is not None
    assert spot.freq * 1e6 == pytest.approx(14_095_600 + 1535.123, abs=1e-4)
    assert spot.metric == pytest.approx(0.012)
    assert (spot.call, spot.grid, spot.power) == ("WB6CXC", "CM88", 30)
    row = spot_to_row(spot.__class__(**{**spot.__dict__, "date": "261028",
                                        "time": "0526", "pkt_mode": 3}),
                      band="20", radiod_id="rx", rx_call="A", rx_grid="EM38",
                      host_id="h")
    assert row["frequency_mhz_full"] == pytest.approx(14.097135123, abs=1e-12)


# ---- 3. fine-frequency flags ------------------------------------------------

# wsprd's usage (ubersdr 262b4db src/wsprd_files.cpp), printed when it is
# run with no arguments.
WSPRD_USAGE_FINE = (
    "Usage: wsprd [options...] infile\n"
    "       -z x (x is fano metric table bias, default is 0.45)\n"
    "       -n no drift, does not attempt to follow a drifting frequency\n"
    "       -F fine frequency: each decode's frequency measured to 0.001 Hz,"
    " ALL_WSPR.TXT's printed %13.9f\n"
    "       -0 streaming mode: read PCM audio from stdin per the\n"
)
WSPRD_USAGE_OLD = WSPRD_USAGE_FINE.replace(
    "       -F fine frequency: each decode's frequency measured to 0.001 Hz,"
    " ALL_WSPR.TXT's printed %13.9f\n", "")
JT9_HELP_FINE = (" OPTIONS:\n \n -h\n --help\n    Display this help message\n"
                 " --fine-freq\n    Ours (FST4W): fst4_decodes.dat's fsig ...\n"
                 " --spread fast|exact|off\n")
JT9_HELP_OLD = JT9_HELP_FINE.replace(
    " --fine-freq\n    Ours (FST4W): fst4_decodes.dat's fsig ...\n", "")


@pytest.fixture
def fake_help(monkeypatch):
    """subprocess.run stand-in: answers help probes from a table and
    records each probe; decode runs return nothing."""
    texts: dict = {}
    probes: list = []
    runs: list = []

    def fake_run(cmd, *a, **kw):
        if cmd[0] in texts and len(cmd) <= 2:
            probes.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1, texts[cmd[0]], "")
        runs.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    decoder_mod._FINE_FREQ_CACHE.clear()
    monkeypatch.setattr(decoder_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(decoder_mod.os.path, "getmtime", lambda p: 1.0)
    yield texts, probes, runs
    decoder_mod._FINE_FREQ_CACHE.clear()


def test_probe_wsprd_fine_flag(fake_help):
    texts, probes, _ = fake_help
    texts["wsprd-new"] = WSPRD_USAGE_FINE
    texts["wsprd-old"] = WSPRD_USAGE_OLD
    assert decoder_mod.wsprd_has_fine_freq("wsprd-new") is True
    assert decoder_mod.wsprd_has_fine_freq("wsprd-old") is False
    # wsprd prints its usage when run with no arguments
    assert probes == [["wsprd-new"], ["wsprd-old"]]


def test_probe_jt9_fine_flag(fake_help):
    texts, probes, _ = fake_help
    texts["jt9-new"] = JT9_HELP_FINE
    texts["jt9-old"] = JT9_HELP_OLD
    assert decoder_mod.jt9_has_fine_freq("jt9-new") is True
    assert decoder_mod.jt9_has_fine_freq("jt9-old") is False
    assert probes == [["jt9-new", "--help"], ["jt9-old", "--help"]]


def test_probe_is_cached_per_binary(fake_help):
    texts, probes, _ = fake_help
    texts["wsprd-new"] = WSPRD_USAGE_FINE
    for _ in range(3):
        assert decoder_mod.wsprd_has_fine_freq("wsprd-new") is True
    assert len(probes) == 1


def test_probe_of_missing_binary_is_false(monkeypatch):
    decoder_mod._FINE_FREQ_CACHE.clear()
    assert decoder_mod.wsprd_has_fine_freq("/nonexistent/wsprd") is False
    assert decoder_mod.jt9_has_fine_freq("/nonexistent/jt9") is False
    decoder_mod._FINE_FREQ_CACHE.clear()


def test_wsprd_command_gets_F_only_when_supported(fake_help):
    texts, _, runs = fake_help
    texts["wsprd-new"] = WSPRD_USAGE_FINE
    texts["wsprd-old"] = WSPRD_USAGE_OLD
    r = _runner()
    r._run_wsprd(Path("x.wav"), "wsprd-new")
    r._run_wsprd(Path("x.wav"), "wsprd-old")
    assert "-F" in runs[0] and runs[0].index("-F") < runs[0].index("-f")
    assert "-F" not in runs[1]


def test_jt9_command_gets_fine_freq_only_when_supported(fake_help, monkeypatch):
    texts, _, runs = fake_help
    texts["jt9-new"] = JT9_HELP_FINE
    texts["jt9-old"] = JT9_HELP_OLD
    monkeypatch.setattr(DecoderRunner, "_check_input", lambda *a: None)
    from wspr_recorder.decode_mode import DecodeMode
    _runner(jt9_path="jt9-new").decode_fst4w(Path("x.wav"), 120, DecodeMode.F2)
    _runner(jt9_path="jt9-old").decode_fst4w(Path("x.wav"), 120, DecodeMode.F2)
    (new,), (old,) = [[c for c in runs if c and "--fst4w" in c and p in c]
                      for p in ("jt9-new", "jt9-old")]
    assert "--fine-freq" in new
    assert "--fine-freq" not in old


def test_wsprd_F_all_wspr_line_parses_sub_hz():
    """``wsprd -F`` writes ALL_WSPR.TXT's frequency %13.9f in %11.7f's
    place; no column moves (ubersdr 262b4db all_wspr_line)."""
    # Formatted with that very format string:
    line = ("%6s %4s %3.0f %5.2f %13.9f  %-22s %2d %5.2f %2d %2d %4d %2d %3d %5u %5d"
            % ("261123", "1200", -20, 0.31, 14.097123456, "G3ZIL IO90 23",
               0, 0.45, 1, 1, 0, 1, 12, 81, 3))
    spot = _runner()._parse_all_wspr_line(line)
    assert spot.freq == 14.097123456
    assert (spot.call, spot.grid, spot.power) == ("G3ZIL", "IO90", 23)
    assert (spot.cycles, spot.metric) == (81, 3.0)
    # the -n spreading build ends in a %6.3f spread column
    spot = _runner()._parse_all_wspr_line(line + " %6.3f" % 0.123, spreading=True)
    assert spot.freq == 14.097123456 and spot.spreading == 0.123
