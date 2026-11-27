"""Filing each decode's carrier .wav into the archive tree.

    ROOT/YYYYMMDD/RECEIVER/BAND/HHMM_MODE_CALL_RFHZ.wav

mirroring wsprdaemon's carrier-archive.sh: the decoder writes CALL_AUDIOHZ.wav
(ubersdr carrier_iq.cpp file_name: "%s_%.3f.wav", the call with every
non-alphanumeric as '_'), RFHZ = dial + audio to 0.001 Hz, and where two
wsprd passes both wrote a call's carrier the -n (spreading) pass's wins.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wspr_recorder import decoder as decoder_mod
from wspr_recorder.callsign_db import CallsignDB
from wspr_recorder.carrier_archive import CarrierArchive, safe_name
from wspr_recorder.decode_mode import DecodeMode
from wspr_recorder.decoder import DecoderRunner

WHEN = datetime(2026, 11, 23, 14, 8, tzinfo=timezone.utc)


def _wav(d: Path, name: str, body: bytes = b"RIFF") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(body)
    return p


def _arch(tmp_path, receiver="AC0G-B1") -> CarrierArchive:
    return CarrierArchive(root=tmp_path / "archive", receiver=receiver)


def _filed(tmp_path):
    root = tmp_path / "archive"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.wav"))


# ---- names ------------------------------------------------------------------

@pytest.mark.parametrize("raw, safe", [
    ("AC0G-B1", "AC0G-B1"),
    ("AC0G/S", "AC0G-S"),
    ("AC0G=S", "AC0G=S"),                  # sigmond's path-safe reporter form
    ("../../etc", "etc"),
    ("a b\tc", "a-b-c"),
    ("", "receiver"),
    ("..", "receiver"),
])
def test_safe_name(raw, safe):
    assert safe_name(raw) == safe


def test_receiver_made_safe(tmp_path):
    assert _arch(tmp_path, "AC0G/S").receiver == "AC0G-S"


# ---- filing -----------------------------------------------------------------

def test_files_one_pass(tmp_path):
    p1 = tmp_path / "p1"
    _wav(p1, "K1ABC_1500.124.wav")
    _wav(p1, "PJ4_K1ABC_1460.000.wav")     # PJ4/K1ABC from the decoder
    n = _arch(tmp_path).file_cycle([p1], band="20", when=WHEN, mode="W2",
                                   dial_hz=14_095_600)
    assert n == 2
    assert _filed(tmp_path) == [
        "20261123/AC0G-B1/20/1408_W2_K1ABC_14097100.124.wav",
        "20261123/AC0G-B1/20/1408_W2_PJ4_K1ABC_14097060.000.wav",
    ]
    assert not p1.exists()                 # the decode's scratch dir is gone


def test_n_pass_carrier_wins(tmp_path):
    """p2 (wsprd's -n / spreading pass) is listed first and wins a call both
    passes wrote; a call only p1 has is still filed."""
    p1, p2 = tmp_path / "p1", tmp_path / "p2"
    _wav(p1, "K1ABC_1500.124.wav", b"p1")
    _wav(p1, "W2XYZ_1420.500.wav", b"p1")
    _wav(p2, "K1ABC_1500.131.wav", b"p2")
    n = _arch(tmp_path).file_cycle([p2, p1], band="20", when=WHEN, mode="W2",
                                   dial_hz=14_095_600)
    assert n == 2
    d = tmp_path / "archive/20261123/AC0G-B1/20"
    assert sorted(p.name for p in d.iterdir()) == [
        "1408_W2_K1ABC_14097100.131.wav", "1408_W2_W2XYZ_14097020.500.wav"]
    assert (d / "1408_W2_K1ABC_14097100.131.wav").read_bytes() == b"p2"
    assert not p1.exists() and not p2.exists()


def test_fst4w_mode_and_band_names(tmp_path):
    p = tmp_path / "f"
    _wav(p, "G3ZIL_1535.123.wav")
    _arch(tmp_path).file_cycle([p], band="630", when=WHEN, mode="F15",
                               dial_hz=474_200)
    assert _filed(tmp_path) == [
        "20261123/AC0G-B1/630/1408_F15_G3ZIL_475735.123.wav"]


def test_files_names_with_the_period(tmp_path):
    """madpsy's proposed CALL_AUDIOHZ_YYMMDD_HHMM.wav (after PR #8) files
    exactly as CALL_AUDIOHZ.wav does: the call is not read as
    CALL_AUDIOHZ_YYMMDD, nor HHMM as the frequency.  Both forms may sit in
    one dir (an older and a newer decoder, the -n pass and the other)."""
    p = tmp_path / "p1"
    _wav(p, "K1ABC_1500.124_261123_1408.wav")
    _wav(p, "PJ4_K1ABC_1460.000_261123_1408.wav")
    _wav(p, "W2XYZ_1420.500.wav")
    n = _arch(tmp_path).file_cycle([p], band="20", when=WHEN, mode="W2",
                                   dial_hz=14_095_600)
    assert n == 3
    assert _filed(tmp_path) == [
        "20261123/AC0G-B1/20/1408_W2_K1ABC_14097100.124.wav",
        "20261123/AC0G-B1/20/1408_W2_PJ4_K1ABC_14097060.000.wav",
        "20261123/AC0G-B1/20/1408_W2_W2XYZ_14097020.500.wav",
    ]


def test_period_name_passes_the_call_filter(tmp_path):
    """The default calls = ["WW0WWV"] keeps WW0WWV's timed name; a misparse
    (call "WW0WWV_1500.049_261123") would have discarded it silently."""
    p = tmp_path / "p1"
    _wav(p, "WW0WWV_1500.049_261123_1408.wav")
    _wav(p, "K1ABC_1500.124_261123_1408.wav")
    arch = CarrierArchive(root=tmp_path / "archive", receiver="AC0G-B1",
                          calls=["WW0WWV"])
    assert arch.file_cycle([p], band="40", when=WHEN, mode="W2",
                           dial_hz=7_038_600) == 1
    assert _filed(tmp_path) == [
        "20261123/AC0G-B1/40/1408_W2_WW0WWV_7040100.049.wav"]


def test_ignores_partial_and_foreign_files(tmp_path):
    p = tmp_path / "p1"
    _wav(p, "K1ABC_1500.124.wav.tmp")      # write_wav's in-progress name
    _wav(p, "notes.txt")
    _wav(p, "nofreq.wav")
    _wav(p, "K1ABC_abc.wav")
    assert _arch(tmp_path).file_cycle([p], band="20", when=WHEN, mode="W2",
                                      dial_hz=14_095_600) == 0
    assert _filed(tmp_path) == []
    assert not p.exists()


def test_none_and_missing_dirs_are_fine(tmp_path):
    assert _arch(tmp_path).file_cycle([None, tmp_path / "nope"], band="20",
                                      when=WHEN, mode="W2", dial_hz=1) == 0


def test_filing_error_is_logged_not_raised(tmp_path, caplog):
    root = tmp_path / "archive"
    root.write_text("a file where the archive dir should be")
    p = tmp_path / "p1"
    _wav(p, "K1ABC_1500.124.wav")
    with caplog.at_level(logging.WARNING):
        n = CarrierArchive(root=root, receiver="R").file_cycle(
            [p], band="20", when=WHEN, mode="W2", dial_hz=14_095_600)
    assert n == 0
    assert "carrier" in caplog.text
    assert not p.exists()                  # scratch still cleaned up


# ---- through the decoder ----------------------------------------------------

USAGE = ("       -F fine frequency: x\n"
         "       -I dir: write each decode's carrier, modulation removed\n")
JT9_HELP = " --fine-freq\n --carrier-dir DIR\n"


@pytest.fixture
def fake_decoders(monkeypatch):
    """wsprd / jt9 stand-ins that write carrier files where -I / --carrier-dir
    point, as the ubersdr build does."""
    writes: dict = {}

    def fake_run(cmd, *a, **kw):
        if len(cmd) <= 2:                                   # help probe
            text = JT9_HELP if "--help" in cmd else USAGE
            return subprocess.CompletedProcess(cmd, 1, text, "")
        for flag in ("-I", "--carrier-dir"):
            if flag in cmd:
                d = Path(cmd[cmd.index(flag) + 1])
                binary = next(c for c in cmd if c.startswith(("wsprd", "jt9")))
                for name, body in writes.get(binary, []):
                    (d / name).write_bytes(body)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    decoder_mod._FINE_FREQ_CACHE.clear()
    monkeypatch.setattr(decoder_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(decoder_mod.os.path, "getmtime", lambda p: 1.0)
    monkeypatch.setattr(DecoderRunner, "_check_input", lambda *a: None)
    yield writes
    decoder_mod._FINE_FREQ_CACHE.clear()


def _runner(tmp_path, **kw):
    return DecoderRunner(band_name="20", frequency_hz=14_095_600,
                         work_dir=tmp_path / "work", callsign_db=CallsignDB(),
                         carrier_archive=_arch(tmp_path), **kw)


def test_decode_wspr_files_with_spread_preference(fake_decoders, tmp_path):
    fake_decoders["wsprd"] = [("K1ABC_1500.124.wav", b"p1"),
                              ("W2XYZ_1420.500.wav", b"p1")]
    fake_decoders["wsprd.spread"] = [("K1ABC_1500.131.wav", b"p2")]
    r = _runner(tmp_path, wsprd_path="wsprd", wsprd_spread_path="wsprd.spread")
    r.decode_wspr(Path("261123_1408.wav"), cycle_start=WHEN.timestamp())
    assert _filed(tmp_path) == [
        "20261123/AC0G-B1/20/1408_W2_K1ABC_14097100.131.wav",
        "20261123/AC0G-B1/20/1408_W2_W2XYZ_14097020.500.wav",
    ]
    assert list((tmp_path / "work" / "carrier.d").iterdir()) == []


def test_decode_wspr_time_from_wav_name_without_cycle_start(fake_decoders,
                                                            tmp_path):
    fake_decoders["wsprd"] = [("K1ABC_1500.124.wav", b"x")]
    _runner(tmp_path, wsprd_path="wsprd").decode_wspr(Path("261123_1408.wav"))
    assert _filed(tmp_path) == [
        "20261123/AC0G-B1/20/1408_W2_K1ABC_14097100.124.wav"]


def test_decode_fst4w_files_with_mode(fake_decoders, tmp_path):
    fake_decoders["jt9"] = [("G3ZIL_1535.123.wav", b"x")]
    _runner(tmp_path, jt9_path="jt9").decode_fst4w(
        Path("261123_1408.wav"), 300, DecodeMode.F5,
        cycle_start=WHEN.timestamp())
    assert _filed(tmp_path) == [
        "20261123/AC0G-B1/20/1408_F5_G3ZIL_14097135.123.wav"]


def test_filing_failure_does_not_break_the_decode(fake_decoders, tmp_path,
                                                  monkeypatch):
    fake_decoders["wsprd"] = [("K1ABC_1500.124.wav", b"x")]

    def boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(CarrierArchive, "file_cycle", boom)
    r = _runner(tmp_path, wsprd_path="wsprd")
    assert r.decode_wspr(Path("261123_1408.wav"),
                         cycle_start=WHEN.timestamp()) == []
    assert r.decode_fst4w(Path("261123_1408.wav"), 300, DecodeMode.F5,
                          cycle_start=WHEN.timestamp()) == []
    # and the scratch dirs are still removed
    assert list((tmp_path / "work" / "carrier.d").iterdir()) == []
