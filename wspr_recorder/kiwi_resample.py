"""Stateful resampler for Kiwi audio to exactly 12000 Hz.

A crystal-clocked KiwiSDR reports ~12001.1 Hz (or e.g. 11999.02 Hz); WSPR
needs exactly 12000 Hz.  The ratio is within ~1e-4 of unity, so a windowed-sinc
fractional-delay interpolator (output sample k = input at time (k - 15)*in/out, a fixed ~1.3 ms delay) is
enough: no decimation filter is needed and a 1500 Hz tone keeps its frequency
to well under 0.01 Hz.  State (input tail + fractional position) is carried
across process() calls, so any chunking gives the same output.

A Kiwi on an external clock reports exactly 12000 Hz and is passed through
untouched (no filtering, no delay).
"""
import numpy as np

EXACT_RATE_TOL_HZ = 0.001

_HALF = 16            # taps = 2 * _HALF
_KAISER_BETA = 8.6


class StreamResampler:
    def __init__(self, in_rate: float, out_rate: int = 12000):
        self.out_rate = out_rate
        self._win = np.kaiser(2 * _HALF + 1, _KAISER_BETA)  # centre-indexed
        self._offsets = np.arange(-_HALF + 1, _HALF + 1)    # taps around floor(p)
        self.set_in_rate(in_rate)

    @property
    def passthrough(self) -> bool:
        return abs(self.in_rate - self.out_rate) <= EXACT_RATE_TOL_HZ

    def set_in_rate(self, in_rate: float) -> None:
        was = getattr(self, "in_rate", None) is not None and self.passthrough
        self.in_rate = float(in_rate)
        self._step = self.in_rate / self.out_rate
        if getattr(self, "_buf", None) is None or was != self.passthrough:
            self._reset()

    def _reset(self) -> None:
        # The kernel needs _HALF samples of look-ahead, which would leave the
        # output ~_HALF samples short of in*out/in_rate.  Start the output
        # clock _HALF-1 input samples early, over a zero pre-roll, so the
        # output count tracks the rate; the cost is a constant ~1.3 ms delay
        # of the audio (irrelevant to WSPR).  _base is the absolute index of
        # _buf[0].
        self._buf = np.zeros(2 * _HALF, dtype=np.float64)
        self._base = -2 * _HALF
        self._pos = float(-(_HALF - 1))  # absolute input position of next output

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.passthrough:
            return x
        buf = np.concatenate([self._buf, np.asarray(x, dtype=np.float64)])
        last = self._base + len(buf) - 1
        # Need floor(p) + _HALF <= last  ->  p < last - _HALF + 1
        limit = last - _HALF + 1
        n = int(np.ceil((limit - self._pos) / self._step)) if limit > self._pos else 0
        p = self._pos + self._step * np.arange(n)
        ip = np.floor(p).astype(np.int64)
        frac = p - ip
        # tap offsets o in [-HALF+1, HALF]; distance from sample to p is o - frac
        d = self._offsets[None, :] - frac[:, None]
        w = np.sinc(d) * np.interp(d, np.arange(-_HALF, _HALF + 1), self._win)
        idx = (ip[:, None] + self._offsets[None, :]) - self._base
        y = np.einsum("ij,ij->i", buf[idx], w) if n else np.empty(0)
        self._pos += self._step * n
        keep_from = int(np.floor(self._pos)) - _HALF + 1
        drop = max(0, keep_from - self._base)
        self._buf = buf[drop:]
        self._base += drop
        return y.astype(np.float32)
