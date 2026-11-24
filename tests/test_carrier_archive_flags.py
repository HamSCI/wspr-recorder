"""Decoder options for the carrier archive: wsprd -I DIR, jt9 --carrier-dir DIR.

Passed only when the archive is enabled AND the binary's help text lists the
option (ubersdr 5eeeb53, madpsy PR #6).  Otherwise the command lines are
exactly today's.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wspr_recorder import decoder as decoder_mod
from wspr_recorder.callsign_db import CallsignDB
from wspr_recorder.carrier_archive import CarrierArchive
from wspr_recorder.decode_mode import DecodeMode
from wspr_recorder.decoder import DecoderRunner

# Verbatim from the ubersdr build's usage / --help (src/wsprd_files.cpp,
# src/fst4w_jt9.cpp), trimmed to the lines around the options.
WSPRD_USAGE_ALL = (
    "Usage: wsprd [options...] infile\n"
    "       -n no drift, does not attempt to follow a drifting frequency\n"
    "       -F fine frequency: each decode's frequency measured to 0.001 Hz,"
    " ALL_WSPR.TXT's printed %13.9f\n"
    "       -I dir: write each decode's carrier, modulation removed, as an IQ"
    " .wav in dir\n"
    "       -0 streaming mode: read PCM audio from stdin per the\n"
)
WSPRD_USAGE_NO_I = WSPRD_USAGE_ALL.replace(
    "       -I dir: write each decode's carrier, modulation removed, as an IQ"
    " .wav in dir\n", "")
JT9_HELP_ALL = (" OPTIONS:\n --fine-freq\n    Ours (FST4W): fsig ...\n"
                " --carrier-dir DIR\n    Ours (FST4W): write each decode's"
                " carrier, its modulation taken off, as a\n")
JT9_HELP_NO_CARRIER = " OPTIONS:\n --fine-freq\n    Ours (FST4W): fsig ...\n"

TODAY_WSPRD = ["-c", "-C", "500", "-o", "4", "-d", "-F",
               "-f", "14.095600", "x.wav"]


@pytest.fixture
def fake(monkeypatch):
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
    monkeypatch.setattr(DecoderRunner, "_check_input", lambda *a: None)
    yield texts, probes, runs
    decoder_mod._FINE_FREQ_CACHE.clear()


def _runner(tmp_path, archive=None, **kw) -> DecoderRunner:
    return DecoderRunner(band_name="20", frequency_hz=14_095_600,
                         work_dir=tmp_path / "work", callsign_db=CallsignDB(),
                         carrier_archive=archive, **kw)


def _archive(tmp_path) -> CarrierArchive:
    return CarrierArchive(root=tmp_path / "archive", receiver="AC0G-B1")


def _decoder_runs(runs, binary):
    return [c for c in runs if binary in c]


# ---- probes -----------------------------------------------------------------

def test_probe_wsprd_I(fake):
    texts, probes, _ = fake
    texts["wsprd-new"] = WSPRD_USAGE_ALL
    texts["wsprd-old"] = WSPRD_USAGE_NO_I
    assert decoder_mod.wsprd_has_carrier_dir("wsprd-new") is True
    assert decoder_mod.wsprd_has_carrier_dir("wsprd-old") is False
    assert probes == [["wsprd-new"], ["wsprd-old"]]


def test_probe_jt9_carrier_dir(fake):
    texts, probes, _ = fake
    texts["jt9-new"] = JT9_HELP_ALL
    texts["jt9-old"] = JT9_HELP_NO_CARRIER
    assert decoder_mod.jt9_has_carrier_dir("jt9-new") is True
    assert decoder_mod.jt9_has_carrier_dir("jt9-old") is False
    assert probes == [["jt9-new", "--help"], ["jt9-old", "--help"]]


def test_probes_do_not_share_a_cache_entry(fake):
    """-F and -I are both read from wsprd's bare usage: one answer must not
    stand in for the other."""
    texts, _, _ = fake
    texts["wsprd-old"] = WSPRD_USAGE_NO_I               # has -F, lacks -I
    assert decoder_mod.wsprd_has_fine_freq("wsprd-old") is True
    assert decoder_mod.wsprd_has_carrier_dir("wsprd-old") is False
    texts["jt9-old"] = JT9_HELP_NO_CARRIER
    assert decoder_mod.jt9_has_fine_freq("jt9-old") is True
    assert decoder_mod.jt9_has_carrier_dir("jt9-old") is False


def test_carrier_probe_cached(fake):
    texts, probes, _ = fake
    texts["wsprd-new"] = WSPRD_USAGE_ALL
    for _ in range(3):
        assert decoder_mod.wsprd_has_carrier_dir("wsprd-new")
    assert probes == [["wsprd-new"]]


# ---- wsprd ------------------------------------------------------------------

def test_wsprd_disabled_is_byte_identical_and_never_probes_I(fake, tmp_path):
    texts, probes, runs = fake
    texts["wsprd-new"] = WSPRD_USAGE_ALL
    _runner(tmp_path, wsprd_path="wsprd-new").decode_wspr(Path("x.wav"))
    assert _decoder_runs(runs, "wsprd-new") == [["wsprd-new", *TODAY_WSPRD]]
    # only the -F probe ran; no -I question was asked
    assert probes == [["wsprd-new"]]
    assert not (tmp_path / "work" / "carrier.d").exists()


def test_wsprd_enabled_probe_fails_is_byte_identical(fake, tmp_path):
    texts, _, runs = fake
    texts["wsprd-old"] = WSPRD_USAGE_NO_I
    _runner(tmp_path, _archive(tmp_path),
            wsprd_path="wsprd-old").decode_wspr(Path("x.wav"))
    assert _decoder_runs(runs, "wsprd-old") == [["wsprd-old", *TODAY_WSPRD]]


def test_wsprd_unrunnable_probe_is_byte_identical(fake, tmp_path, monkeypatch):
    _, _, runs = fake
    # no help text registered: the probe "fails"; the decode run still records
    monkeypatch.setattr(decoder_mod, "_help_text", lambda cmd: None)
    _runner(tmp_path, _archive(tmp_path),
            wsprd_path="wsprd-x").decode_wspr(Path("x.wav"))
    assert _decoder_runs(runs, "wsprd-x") == [
        ["wsprd-x", "-c", "-C", "500", "-o", "4", "-d",
         "-f", "14.095600", "x.wav"]]


def test_wsprd_enabled_gets_I_dir_before_f(fake, tmp_path):
    texts, _, runs = fake
    texts["wsprd-new"] = WSPRD_USAGE_ALL
    _runner(tmp_path, _archive(tmp_path),
            wsprd_path="wsprd-new").decode_wspr(Path("x.wav"))
    (cmd,) = _decoder_runs(runs, "wsprd-new")
    i = cmd.index("-I")
    assert cmd[:i] == ["wsprd-new", "-c", "-C", "500", "-o", "4", "-d", "-F"]
    assert cmd[i + 2:] == ["-f", "14.095600", "x.wav"]
    carrier_dir = Path(cmd[i + 1])
    assert carrier_dir.is_absolute()
    assert carrier_dir.parent == tmp_path / "work" / "carrier.d"
    # the per-decode directory is gone once the decode returns
    assert not carrier_dir.exists()


def test_wsprd_spread_pass_gets_its_own_dir(fake, tmp_path):
    texts, _, runs = fake
    texts["wsprd-new"] = WSPRD_USAGE_ALL
    texts["wsprd-spread"] = WSPRD_USAGE_ALL
    _runner(tmp_path, _archive(tmp_path), wsprd_path="wsprd-new",
            wsprd_spread_path="wsprd-spread").decode_wspr(Path("x.wav"))
    (p1,) = _decoder_runs(runs, "wsprd-new")
    (p2,) = _decoder_runs(runs, "wsprd-spread")
    assert p1[p1.index("-I") + 1] != p2[p2.index("-I") + 1]


# ---- jt9 --------------------------------------------------------------------

TODAY_JT9_TAIL = ["-p", "300", "-f", "1500", "-F", "100", "x.wav"]


def _jt9_cmd(runs, binary):
    (cmd,) = [c for c in runs if binary in c and "--fst4w" in c]
    return cmd


def test_jt9_disabled_is_byte_identical(fake, tmp_path):
    texts, probes, runs = fake
    texts["jt9-new"] = JT9_HELP_ALL
    r = _runner(tmp_path, jt9_path="jt9-new")
    r.decode_fst4w(Path("x.wav"), 300, DecodeMode.F5)
    assert _jt9_cmd(runs, "jt9-new") == [
        "nice", "-n", "19", "timeout", "300", "jt9-new", "-Y",
        "-a", str(r.work_dir), "--fst4w", "--fine-freq", *TODAY_JT9_TAIL]
    assert probes == [["jt9-new", "--help"]]             # --fine-freq's only


def test_jt9_enabled_probe_fails_is_byte_identical(fake, tmp_path):
    texts, _, runs = fake
    texts["jt9-old"] = JT9_HELP_NO_CARRIER
    r = _runner(tmp_path, _archive(tmp_path), jt9_path="jt9-old")
    r.decode_fst4w(Path("x.wav"), 300, DecodeMode.F5)
    assert _jt9_cmd(runs, "jt9-old") == [
        "nice", "-n", "19", "timeout", "300", "jt9-old", "-Y",
        "-a", str(r.work_dir), "--fst4w", "--fine-freq", *TODAY_JT9_TAIL]


def test_jt9_enabled_gets_carrier_dir_after_fine_freq(fake, tmp_path):
    texts, _, runs = fake
    texts["jt9-new"] = JT9_HELP_ALL
    r = _runner(tmp_path, _archive(tmp_path), jt9_path="jt9-new")
    r.decode_fst4w(Path("x.wav"), 300, DecodeMode.F5)
    cmd = _jt9_cmd(runs, "jt9-new")
    i = cmd.index("--carrier-dir")
    assert cmd[:i] == ["nice", "-n", "19", "timeout", "300", "jt9-new", "-Y",
                       "-a", str(r.work_dir), "--fst4w", "--fine-freq"]
    assert cmd[i + 2:] == TODAY_JT9_TAIL
    d = Path(cmd[i + 1])
    assert d.parent == tmp_path / "work" / "carrier.d" and not d.exists()
