"""WW0WWV's carrier against WWV's: the physics behind the carrier browser's main page.

WW0WWV is the WSPR beacon at the WWV site, locked to the same standard as
WWV.  Its carrier (from the carrier archive, its modulation removed) and
WWV's own carrier cross the same ionosphere on nearly the same path, so
their Doppler, in parts per billion of each nominal frequency, should
agree; where they do not, one of them is lying.

Ported from Rob's KPH study scripts (fst4w-freq-test/kph: carrier-read.py,
doppler-compare.py, day-chart.py, and the ww0wwv-cycles.py results they
read):

  * a carrier's frequency is the peak of its periodogram, zero-padded 64
    times, refined by a parabola through the top three bins
    (``freq_fit``); its RF is ``dial_mhz * 1e6 + ref_hz`` (the decoder's
    audio frequency, from the file's JSON) plus that baseband peak;
  * Doppler, ppb = (RF - nominal) / nominal * 1e9 against WW0WWV's own
    transmit frequency on the band (``NOMINAL_HZ``; for 12 m the WS-8's
    24926049.919536 Hz, not the 24926050.049 Hz of the first tables);
  * a track: windows of 10 s (30 s when wsprd's SNR is -15 dB or less)
    every 2 s, each window's peak searched within 2 Hz of the whole
    transmission's;
  * SNR in 2500 Hz: the power within 0.5 Hz of the peak over the mean
    density of the bins 3 Hz or more away.

WWV's side comes from hf-timestd (``TimestdWwv``): its L2 tick-timing
product, one Doppler a minute per channel and station, in the SQLite
``timestd.db``.  Anything else that can answer ``WwvSource.series`` can
stand in for it.

Standard library and numpy only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import struct
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

CALL = "WW0WWV"

# WW0WWV's transmit frequency (WSPR centre) per band, Hz: the table
# ww0wwv-cycles.py used (its records' nominal_hz).  12 m is the WS-8's own
# value; before 2026-11-16 the scripts had 24926050.049 Hz, 5.19 ppb off.
NOMINAL_HZ: Dict[str, float] = {
    "80": 3570050.145432,
    "40": 7040050.130209,
    "30": 10140150.146485,
    "20": 14097050.048829,
    "15": 21096050.048829,
    "12": 24926049.919536,
    "10": 28126050.109966,
}

# Each WW0WWV band and the WWV frequency, MHz, it is compared with: the
# nearest WWV carrier above it, as in the KPH study.  (The study also had
# 80 m with 2.5 MHz and 40 m with 5 MHz; the beta shows these five.)
BAND_PAIRS: List[Tuple[str, float]] = [
    ("30", 10.0),
    ("20", 15.0),
    ("15", 20.0),
    ("12", 25.0),
    ("10", 25.0),
]
WWV_FOR_BAND: Dict[str, float] = dict(BAND_PAIRS)

WIN_S, WEAK_WIN_S, STEP_S, WEAK_SNR_DB = 10.0, 30.0, 2.0, -15.0


# ---- the carrier file --------------------------------------------------------

def read_carrier(path: str) -> Tuple[dict, int, np.ndarray]:
    """(JSON, sample rate, complex IQ) of a carrier .wav (2-channel float32,
    the decode's JSON in LIST/INFO/ICMT).  Raises ValueError if it is not
    one."""
    with open(path, "rb") as f:
        b = f.read(16 * 1024 * 1024)
    if b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        raise ValueError("not a WAV")
    i, meta, rate, iq = 12, {}, None, None
    while i + 8 <= len(b):
        cid, n = b[i:i + 4], struct.unpack("<I", b[i + 4:i + 8])[0]
        body = b[i + 8:i + 8 + n]
        if cid == b"fmt ":
            fmt, ch, rate = struct.unpack("<HHI", body[:8])
            if fmt != 3 or ch != 2:
                raise ValueError("not 2-channel float")
        elif cid == b"LIST" and body[:4] == b"INFO":
            j = 4
            while j + 8 <= len(body):
                sid, m = body[j:j + 4], struct.unpack("<I", body[j + 4:j + 8])[0]
                if sid == b"ICMT":
                    try:
                        meta = json.loads(body[j + 8:j + 8 + m].split(b"\0")[0] or b"{}")
                    except ValueError:
                        meta = {}
                j += 8 + m + (m & 1)
        elif cid == b"data":
            a = np.frombuffer(body[:len(body) // 8 * 8], dtype="<f4")
            iq = a[0::2].astype(np.float64) + 1j * a[1::2].astype(np.float64)
        i += 8 + n + (n & 1)
    if rate is None or iq is None:
        raise ValueError("no fmt or data chunk")
    return (meta if isinstance(meta, dict) else {}), int(rate), iq


def _parabola(S: np.ndarray, k: int) -> float:
    n = len(S)
    a, b, c = S[k - 1], S[k], S[(k + 1) % n]
    den = a - 2 * b + c
    return 0.5 * (a - c) / den if den != 0 else 0.0


def freq_fit(z: np.ndarray, rate: float) -> float:
    """Baseband frequency, Hz, of the carrier in ``z``: the periodogram peak
    (zero-padded 64 times, parabola through the top three points)."""
    n = 1 << int(np.ceil(np.log2(len(z) * 64)))
    S = np.abs(np.fft.fft(z, n)) ** 2
    k = int(np.argmax(S))
    f = (k + _parabola(S, k)) * rate / n
    return f - rate if f >= rate / 2 else f


def peak_near(z: np.ndarray, rate: float, centre: float, span: float) -> float:
    """As freq_fit, the peak searched only within ``span`` Hz of ``centre``."""
    n = 1 << int(np.ceil(np.log2(len(z) * 64)))
    S = np.abs(np.fft.fft(z, n)) ** 2
    f = np.fft.fftfreq(n, 1 / rate)
    idx = np.where(np.abs(f - centre) <= span)[0]
    if not len(idx):
        return freq_fit(z, rate)
    k = int(idx[np.argmax(S[idx])])
    return float(f[k] + _parabola(S, k) * rate / n)


def snr_db(z: np.ndarray, rate: float) -> float:
    """Carrier power over the noise in 2500 Hz, dB."""
    n = len(z)
    S = np.abs(np.fft.fft(z * np.hanning(n))) ** 2
    f = np.fft.fftfreq(n, 1 / rate)
    k = int(np.argmax(S))
    near = np.abs(f - f[k]) < 0.5
    far = np.abs(f - f[k]) > 3.0
    if not far.any():
        return float("nan")
    noise = float(np.mean(S[far]))
    sig = float(S[near].sum() - noise * near.sum())
    if noise <= 0:
        return float("nan")
    return float(10 * np.log10(max(sig, 1e-30) / (noise / (rate / n) * 2500)))


def rf_ref_hz(meta: dict, fallback_rf_hz: Optional[float] = None) -> Optional[float]:
    """The RF the carrier's 0 Hz stands for: ``dial_mhz * 1e6 + ref_hz``
    from the decode's JSON, else ``fallback_rf_hz`` (the file name's)."""
    try:
        return float(meta["dial_mhz"]) * 1e6 + float(meta["ref_hz"])
    except (KeyError, TypeError, ValueError):
        return fallback_rf_hz


def ppb(rf_hz: float, nominal_hz: float) -> float:
    return (rf_hz - nominal_hz) / nominal_hz * 1e9


def carrier_summary(meta: dict, rate: int, iq: np.ndarray, band: str,
                    fallback_rf_hz: Optional[float] = None) -> Optional[dict]:
    """The whole transmission: its RF, its offset from WW0WWV's nominal on
    ``band`` in Hz and ppb, and its carrier SNR.  None if the band has no
    nominal or the RF cannot be known."""
    nominal = NOMINAL_HZ.get(band)
    ref = rf_ref_hz(meta, fallback_rf_hz)
    if nominal is None or ref is None or len(iq) < 16:
        return None
    rf = ref + freq_fit(iq, rate)
    return {"rf_hz": rf, "nominal_hz": nominal, "hz": rf - nominal,
            "ppb": ppb(rf, nominal), "snr_db": snr_db(iq, rate)}


def carrier_track(meta: dict, rate: int, iq: np.ndarray, band: str,
                  cycle_epoch: float,
                  fallback_rf_hz: Optional[float] = None) -> Optional[dict]:
    """The Doppler track over the transmission: windows of 10 s (30 s when
    wsprd's SNR is -15 dB or less) every 2 s; each point's epoch (the
    window's centre), ppb and SNR."""
    nominal = NOMINAL_HZ.get(band)
    ref = rf_ref_hz(meta, fallback_rf_hz)
    if nominal is None or ref is None:
        return None
    try:
        weak = float(meta.get("snr")) <= WEAK_SNR_DB
    except (TypeError, ValueError):
        weak = False
    win = WEAK_WIN_S if weak else WIN_S
    nw, ns = int(win * rate), int(STEP_S * rate)
    if len(iq) < nw or nw < 8:
        return {"win_s": win, "points": []}
    whole = freq_fit(iq, rate)
    t0 = cycle_epoch + float(meta.get("t0_s") or 0.0)
    pts = []
    for s in range(0, len(iq) - nw + 1, ns):
        z = iq[s:s + nw]
        rf = ref + peak_near(z, rate, whole, 2.0)
        pts.append({"t": round(t0 + (s + nw / 2) / rate, 3),
                    "ppb": round(ppb(rf, nominal), 4),
                    "snr": round(snr_db(z, rate), 2)})
    return {"win_s": win, "points": pts}


# ---- WWV ---------------------------------------------------------------------

class WwvSource:
    """Where WWV's carrier Doppler comes from.  ``series(mhz, t0, t1)``
    returns ``{"available": bool, "reason": str, "source": str,
    "channel": str|None, "points": [{"t": epoch, "ppb": float,
    "hz": float, "snr": float|None}]}``: WWV's Doppler, in ppb of
    ``mhz``, between epochs t0 and t1.  This one has none."""

    name = "none"

    def series(self, mhz: float, t0: float, t1: float) -> dict:
        return {"available": False, "reason": "no WWV data source configured",
                "source": self.name, "channel": None, "points": []}


class TimestdWwv(WwvSource):
    """WWV's Doppler from hf-timestd's L2 tick-timing product.

    hf-timestd writes, per channel, station and UTC minute, one row of
    table ``L2_tick_timing`` in its SQLite database (default
    ``/var/lib/timestd/phase2/timestd.db``, its ``[storage] sqlite_path``):
    ``minute_boundary_utc`` (epoch of the minute), ``channel``
    (``WWV_20000``, ``SHARED_10000`` ...), ``station`` (``WWV``,
    ``WWVH`` ...), ``frequency_mhz``, ``doppler_hz`` (the carrier phase
    slope across the minute; NULL when too few ticks) and
    ``mean_snr_db`` (per-tick matched-filter SNR).  Only ``station =
    'WWV'`` rows are used.  The database is opened read-only, per query.
    """

    name = "hf-timestd"
    TABLE = "L2_tick_timing"

    def __init__(self, db_path: str, channels: Optional[Dict[float, Sequence[str]]] = None,
                 cache_s: float = 60.0):
        self.db_path = db_path
        self.channels = channels or {}
        self.cache_s = cache_s
        self._cache: Dict[tuple, Tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _channel_names(self, mhz: float) -> List[str]:
        if mhz in self.channels:
            return list(self.channels[mhz])
        khz = int(round(mhz * 1000))
        return [f"WWV_{khz}", f"SHARED_{khz}"]

    def _none(self, reason: str) -> dict:
        return {"available": False, "reason": reason, "source": self.name,
                "channel": None, "points": []}

    def series(self, mhz: float, t0: float, t1: float) -> dict:
        key = (mhz, int(t0), int(t1))
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.cache_s:
                return hit[1]
        out = self._query(mhz, t0, t1)
        with self._lock:
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[key] = (now, out)
        return out

    def _query(self, mhz: float, t0: float, t1: float) -> dict:
        if not os.path.isfile(self.db_path):
            return self._none(f"no hf-timestd database at {self.db_path}")
        names = self._channel_names(mhz)
        try:
            uri = "file:" + os.path.abspath(self.db_path) + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=2)
        except sqlite3.Error as exc:
            return self._none(f"cannot open {self.db_path}: {exc}")
        try:
            q = (f"SELECT channel, minute_boundary_utc, doppler_hz, mean_snr_db "
                 f"FROM {self.TABLE} WHERE channel IN ({','.join('?' * len(names))}) "
                 f"AND station = 'WWV' AND minute_boundary_utc >= ? "
                 f"AND minute_boundary_utc < ? AND doppler_hz IS NOT NULL "
                 f"ORDER BY minute_boundary_utc")
            rows = con.execute(q, (*names, int(t0), int(t1))).fetchall()
        except sqlite3.Error as exc:
            return self._none(f"cannot read {self.TABLE} from {self.db_path}: {exc}")
        finally:
            con.close()
        if not rows:
            return self._none(f"no WWV {mhz:g} MHz Doppler from hf-timestd "
                              f"({' or '.join(names)}) in this period")
        # one channel: the one with the most rows (a station records a
        # frequency once; this only matters if it has two names)
        count: Dict[str, int] = {}
        for r in rows:
            count[r[0]] = count.get(r[0], 0) + 1
        ch = max(count, key=lambda c: (count[c], c))
        nominal = mhz * 1e6
        pts, seen = [], set()
        for c, t, hz, snr in rows:
            if c != ch or t in seen:
                continue
            try:
                hz = float(hz)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(hz):
                continue
            seen.add(t)
            pts.append({"t": int(t), "hz": round(hz, 6),
                        "ppb": round(hz / nominal * 1e9, 4),
                        "snr": None if snr is None else round(float(snr), 2)})
        return {"available": bool(pts), "reason": "" if pts else "no usable rows",
                "source": self.name, "channel": ch, "points": pts}


def wwv_over(series: dict, t0: float, t1: float) -> Optional[float]:
    """Mean WWV ppb over the minutes starting in [t0, t1), or None."""
    v = [p["ppb"] for p in series.get("points", []) if t0 <= p["t"] < t1]
    return round(float(np.mean(v)), 4) if v else None
