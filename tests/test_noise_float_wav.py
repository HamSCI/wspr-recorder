"""Float32 decoder WAVs: the noise pass must read them and land on
wsprdaemon's calibrated scale (KJ6MKI 2026-10-28)."""
import struct
import numpy as np
import pytest

from wspr_recorder.noise import (
    read_wav_mono_float, compute_noise, compute_rms_noise,
    _FLOAT_WAV_LEVEL_ADJUST, _RMS_NL_ADJUST, _RMS_CAL_OFFSET,
)


def _float_wav(path, samples, sr=12000):
    data = samples.astype("<f4").tobytes()
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 38 + len(data)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHHH", 18, 3, 1, sr, sr * 4, 4, 32, 0))
        f.write(b"data" + struct.pack("<I", len(data)) + data)


def _int16_wav(path, samples, sr=12000):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((samples * 32767).astype("<i2").tobytes())


def test_reads_float_and_int16_wavs_identically(tmp_path):
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(120 * 12000) * 1e-4).astype(np.float32)
    _float_wav(tmp_path / "f.wav", x)
    _int16_wav(tmp_path / "i.wav", x * 1000)        # scaled so int16 keeps it
    sr, xf = read_wav_mono_float(tmp_path / "f.wav")
    assert sr == 12000 and xf is not None and np.allclose(xf, x)
    sr, xi = read_wav_mono_float(tmp_path / "i.wav")
    assert sr == 12000 and xi is not None and np.allclose(xi, x * 1000, atol=2 / 32768)
    assert read_wav_mono_float(tmp_path / "missing.wav") == (0, None)


def test_float_wav_adds_legacy_equivalent_60_db(tmp_path):
    rng = np.random.default_rng(2)
    x = (rng.standard_normal(120 * 12000) * 1e-4).astype(np.float32)
    rms = compute_rms_noise(x, 12000)
    base = compute_noise(samples=x, sample_rate=12000, c2_path=None, band="20")
    flt = compute_noise(samples=x, sample_rate=12000, c2_path=None, band="20",
                        float_wav=True)
    assert flt.rms_noise_dbm == pytest.approx(base.rms_noise_dbm + _FLOAT_WAV_LEVEL_ADJUST)
    # wsprdaemon: RMS_dBFS + (-74.31) - (-peak_dBFS - 61) on a WAV normalised
    # to -1 dBFS == RMS_dBFS(float) - 14.31, then the band offset.
    rms_dbfs = 20 * np.log10(np.sqrt(np.mean(x[3000:9000].astype(np.float64) ** 2)))
    assert flt.rms_noise_dbm == pytest.approx(
        rms_dbfs - 14.31 + _RMS_CAL_OFFSET["20"], abs=1.0)
