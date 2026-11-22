import numpy as np
import pytest

from wspr_recorder.kiwi_resample import StreamResampler


def _run(r, x, nchunks):
    return np.concatenate([r.process(c) for c in np.array_split(x, nchunks)])


@pytest.mark.parametrize("rate", [12001.135, 11999.020338])
def test_1500hz_tone_stays_1500hz(rate):
    r = StreamResampler(rate)
    n = int(120 * rate)
    t = np.arange(n) / rate
    y = _run(r, np.sin(2 * np.pi * 1500 * t).astype(np.float32), 977)
    f = np.fft.rfftfreq(len(y), 1 / 12000)[np.argmax(np.abs(np.fft.rfft(y)))]
    assert abs(f - 1500.0) < 0.01


@pytest.mark.parametrize("rate", [12001.135, 11999.020338])
def test_output_count_tracks_rate(rate):
    r = StreamResampler(rate)
    n_in = int(rate) * 120
    n_out = sum(len(r.process(c)) for c in np.array_split(np.zeros(n_in, np.float32), 300))
    assert abs(n_out - round(n_in * 12000 / rate)) <= 2


def test_output_is_float32():
    assert StreamResampler(12001.135).process(np.zeros(512, np.float32)).dtype == np.float32


def test_chunking_invariant():
    rng = np.random.default_rng(1)
    t = np.arange(24000) / 12001.135
    x = (np.sin(2 * np.pi * 1500 * t) + 0.1 * rng.standard_normal(len(t))).astype(np.float32)
    a = StreamResampler(12001.135).process(x)
    b = _run(StreamResampler(12001.135), x, 1000)
    n = min(len(a), len(b))
    assert abs(len(a) - len(b)) <= 1
    assert np.max(np.abs(a[:n] - b[:n])) < 1e-4


def test_exact_12000_is_passthrough():
    r = StreamResampler(12000.0)
    x = np.random.randn(5000).astype(np.float32)
    assert r.passthrough and np.array_equal(r.process(x), x)


def test_rate_change_switches_mode():
    r = StreamResampler(12000.0)
    assert r.passthrough
    r.set_in_rate(12001.135)
    assert not r.passthrough
    n_in = 12001 * 60
    n_out = sum(len(r.process(c)) for c in np.array_split(np.zeros(n_in, np.float32), 100))
    assert abs(n_out - round(n_in * 12000 / 12001.135)) <= 2
    r.set_in_rate(12000.0)
    assert r.passthrough
