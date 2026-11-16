"""Stock WSJT-X wsprd / jt9 read only int16 PCM WAVs.

sigmond installs two decoder sets under
/usr/local/libexec/sigmond/decoders/{ubersdr,wsjtx}/ and points
/usr/local/bin/{wsprd,jt9} at one of them.  Only the UberSDR build reads
IEEE-float WAVs (format tag 3).  Stock wsprd skips a fixed 44-byte header
and reads the float payload as int16, so a station that fell back to the
wsjtx set (WWV-2, 2026-11-16) decoded nothing and reported nothing.

These tests pin both halves of the fix:
  * the recorder writes int16 WAVs unless the selected decoders read float;
  * a decoder handed a WAV it cannot read fails LOUDLY instead of running.
"""

import logging
import os
import struct
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from wspr_recorder.band_recorder import DecodeRequest
from wspr_recorder.callsign_db import CallsignDB
from wspr_recorder.decode_mode import DecodeMode
from wspr_recorder.decoder import (
    DecoderInputError,
    DecoderRunner,
    decoder_reads_float,
    decoders_read_float,
    wav_format_tag,
)
from wspr_recorder.wav_writer import WavWriter


def _decoder_set(tmp_path: Path, impl: str) -> Path:
    """Build a fake sigmond decoder layout with `impl` selected; return the
    bin dir.  Each fake binary records that it ran."""
    libexec = tmp_path / "libexec" / "sigmond" / "decoders" / impl
    libexec.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for b in ("wsprd", "jt9"):
        exe = libexec / b
        exe.write_text(f"#!/bin/sh\ntouch {tmp_path}/{b}.ran\n")
        exe.chmod(0o755)
        (bindir / b).symlink_to(f"../libexec/sigmond/decoders/{impl}/{b}")
    return bindir


def _float_wav(path: Path, n: int = 1200) -> Path:
    data = (np.sin(np.arange(n) / 10.0) * 0.1).astype("<f4").tobytes()
    fmt = struct.pack("<HHIIHHH", 3, 1, 12000, 48000, 4, 32, 0)
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def _request(n: int = 12000 * 120) -> DecodeRequest:
    return DecodeRequest(
        band_name="20",
        frequency_hz=14_095_600,
        period_seconds=120,
        modes=[DecodeMode.W2],
        start_wallclock=datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc),
        start_rtp_timestamp=0,
        end_rtp_timestamp=n,
        samples=(np.sin(np.arange(n) / 10.0) * 0.1).astype(np.float32),
        gaps=[],
    )


# ---------------------------------------------------------------- detection

@pytest.mark.parametrize("impl,expected", [("ubersdr", True), ("wsjtx", False)])
def test_decoder_reads_float_follows_the_selected_set(tmp_path, impl, expected):
    bindir = _decoder_set(tmp_path, impl)
    assert decoder_reads_float(str(bindir / "wsprd")) is expected
    assert decoder_reads_float(str(bindir / "jt9")) is expected


def test_unknown_decoder_is_assumed_int16_only(tmp_path):
    exe = tmp_path / "wsprd"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert decoder_reads_float(str(exe)) is False
    assert decoder_reads_float("wsprd-not-on-path") is False


@pytest.mark.parametrize("impl,expected", [("ubersdr", True), ("wsjtx", False)])
def test_decoders_read_float_resolves_off_path(tmp_path, monkeypatch, impl, expected):
    bindir = _decoder_set(tmp_path, impl)
    monkeypatch.setenv("PATH", str(bindir))
    assert decoders_read_float() is expected


# ---------------------------------------------------------------- the writer

def test_writer_falls_back_to_int16_when_decoders_cannot_read_float(tmp_path):
    w = WavWriter(tmp_path / "out", sample_format="float32",
                  float_capable=lambda: False)
    wav = w.write_period(_request())
    assert wav is not None
    assert wav_format_tag(wav) == 1          # PCM int16, readable by WSJT-X
    import json
    assert json.loads(wav.with_suffix(".json").read_text())["sample_format"] == "int16"


def test_writer_keeps_float32_when_decoders_read_float(tmp_path):
    w = WavWriter(tmp_path / "out", sample_format="float32",
                  float_capable=lambda: True)
    wav = w.write_period(_request())
    assert wav_format_tag(wav) == 3


def test_writer_int16_config_never_writes_float(tmp_path):
    w = WavWriter(tmp_path / "out", sample_format="int16",
                  float_capable=lambda: True)
    assert wav_format_tag(w.write_period(_request())) == 1


# ---------------------------------------------------------------- loud failure

def _runner(tmp_path: Path, bindir: Path) -> DecoderRunner:
    return DecoderRunner(
        band_name="20", frequency_hz=14_095_600,
        work_dir=tmp_path / "work",
        callsign_db=CallsignDB(tmp_path / "calls.db"),
        wsprd_path=str(bindir / "wsprd"),
        jt9_path=str(bindir / "jt9"),
    )


def test_wsjtx_wsprd_refuses_float_wav_loudly(tmp_path, caplog):
    bindir = _decoder_set(tmp_path, "wsjtx")
    runner = _runner(tmp_path, bindir)
    wav = _float_wav(tmp_path / "261116_1200.wav")
    with caplog.at_level(logging.ERROR), pytest.raises(DecoderInputError):
        runner.decode_wspr(wav)
    assert not (tmp_path / "wsprd.ran").exists()
    assert any(r.levelno >= logging.ERROR and "float" in r.getMessage()
               for r in caplog.records)


def test_wsjtx_jt9_refuses_float_wav_loudly(tmp_path, caplog):
    bindir = _decoder_set(tmp_path, "wsjtx")
    runner = _runner(tmp_path, bindir)
    wav = _float_wav(tmp_path / "261116_1200.wav")
    with caplog.at_level(logging.ERROR), pytest.raises(DecoderInputError):
        runner.decode_fst4w(wav, period=120, mode=DecodeMode.F2)
    assert not (tmp_path / "jt9.ran").exists()
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_ubersdr_wsprd_accepts_float_wav(tmp_path):
    bindir = _decoder_set(tmp_path, "ubersdr")
    runner = _runner(tmp_path, bindir)
    wav = _float_wav(tmp_path / "261116_1200.wav")
    runner.decode_wspr(wav)
    assert (tmp_path / "wsprd.ran").exists()


def test_recorder_wires_the_probe_into_its_writer():
    """The recorder's WavWriter must ask the decoders, or a float32 config
    silences a wsjtx-set station again."""
    import inspect
    import wspr_recorder.__main__ as m
    src = inspect.getsource(m)
    assert "float_capable=decoders_read_float" in src
    assert 'getattr(self.wav_writer, "sample_format"' not in src
