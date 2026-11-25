"""WWV's carrier Doppler measured from hf-timestd's raw IQ buffers.

hf-timestd keeps the IQ of every WWV channel it records, ten minutes to a
file, under ``/var/lib/timestd/raw_buffer/<CHANNEL>/<YYYYMMDD>/<epoch>``
(``.bin.zst``: zstd-compressed little-endian complex64; ``.json``: the
file's particulars, ``minute_boundary``, ``sample_rate``,
``frequency_hz``, ``completeness_pct`` ...), with a live copy in
``/dev/shm/timestd/raw_buffer``.  radiod tunes each channel to the
carrier's nominal frequency (``frequency_hz``, e.g. 20000000), so WWV's
carrier sits at 0 Hz and its baseband frequency *is* its offset from
nominal: the Doppler (plus ``frequency_hz - nominal``, 0 on every channel
hf-timestd makes).

The method is the KPH session's (wwvcar.py, 2026-11-25), which found WWV
60-86 dB above the noise:

  * boxcar-average blocks of 240 samples: 24 kHz to 100 Hz (the boxcar's
    nulls at multiples of 100 Hz also sink WWV's 100 Hz time code and its
    500/600/1000 Hz tones);
  * per UTC minute, 6000 samples, Hann window, 65536-point FFT;
  * the peak within +-1.5 Hz of the carrier, refined by a parabola through
    the log power of the top three bins;
  * SNR: that peak over the median power 3-20 Hz from the carrier, dB (per
    1/60 Hz bin, not per 2500 Hz: WWV's numbers are 60-90 dB);
  * a second peak within +-1.5 Hz, at least 0.05 Hz (3 of the minute's
    1/60 Hz bins) from the first, no more than ``TWO_CARRIER_DB`` below it
    and separated from it by a dip of 3 dB or more, is reported too
    (``two``): on 2.5, 5, 10 and 15 MHz WWVH shares the frequency, so a
    single peak there may be the two stations blended.

hf-timestd's own L2 ``doppler_hz`` was used before; on hf-timestd 5.0.0 it
is noise (KPH1, 24 h, every channel: mean ~0, std 0.04 Hz, lag-1
autocorrelation 0.00, uncorrelated with the carrier), so ``TimestdWwv`` is
now only a labelled fallback for a station without raw buffers.

Cost and memory.  A ten-minute file is ~100 MB of zstd; it is streamed
through ``/usr/bin/zstd -dc`` (the browser's venv has no ``zstandard``)
and decimated chunk by chunk, so no more than ``CHUNK_SAMPLES`` complex64
(~7.7 MB) plus the 100 Hz result (60000 complex128, ~1 MB) is held at
once.  Each file is measured once: its minutes go into a small SQLite
cache (``wwv-carrier.sqlite`` in the cache directory, ~1 MB a day for six
channels), keyed by the file's size and mtime.  A request measures at most
``SYNC_FILES`` files itself (a drill-down); anything more is queued for
one background thread, one file at a time, which also keeps the current
UTC day filled in at a capped duty cycle.  Concurrent requests for one
file share one measurement.

Standard library and numpy only.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import re
import resource
import shutil
import sqlite3
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .ww0wwv import BAND_PAIRS, WwvSource

logger = logging.getLogger(__name__)

DEFAULT_ROOTS = ("/var/lib/timestd/raw_buffer", "/dev/shm/timestd/raw_buffer")
DEFAULT_CACHE_DIR = "/var/cache/wspr-carrier-browser"
CACHE_FILE = "wwv-carrier.sqlite"
SCHEMA = 1

SEARCH_HZ = 1.5             # the peak is searched within +-1.5 Hz of nominal
OUT_RATE = 100.0            # Hz after the boxcar
NFFT = 1 << 16
NOISE_HZ = (3.0, 20.0)      # noise: median power this far from the carrier
MIN_SNR_DB = 10.0           # below: "weak", not used
EDGE_FRACTION = 0.9         # a peak beyond 90 % of the search window: "edge"
TWO_CARRIER_DB = 10.0       # a second peak no more than this below the first
TWO_CARRIER_SEP_HZ = 0.05   # ... and at least this far from it (3 bins of 1/60 Hz)
TWO_CARRIER_DIP_DB = 3.0    # ... with a dip of this much between them
GAPS_PCT = 99.0             # a file less complete than this: minutes "gaps"
CHUNK_SAMPLES = 240 * 4000  # complex64 read at once: 7.7 MB
SYNC_FILES = 2              # a request measures at most this many files itself
FILE_S = 600                # hf-timestd's files: 10 minutes
KEEP_DAYS = 30              # cache rows kept (raw buffers last ~2 days)
NAME_RE = re.compile(r"^(\d{9,11})\.bin\.zst$")


# ---- single flight -----------------------------------------------------------

class SingleFlight:
    """Concurrent calls with one key share one computation: the first runs
    it, the rest wait for its result (or its exception)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running: Dict[object, list] = {}

    def do(self, key, fn: Callable):
        with self._lock:
            slot = self._running.get(key)
            leader = slot is None
            if leader:
                slot = self._running[key] = [threading.Event(), None, None]
        if not leader:
            slot[0].wait()
            if slot[2] is not None:
                raise slot[2]
            return slot[1]
        try:
            slot[1] = fn()
            return slot[1]
        except BaseException as exc:
            slot[2] = exc
            raise
        finally:
            with self._lock:
                self._running.pop(key, None)
            slot[0].set()


# ---- decimation and measurement ---------------------------------------------

def decimate_stream(f, factor: int, max_samples: int,
                    chunk_samples: int = CHUNK_SAMPLES) -> np.ndarray:
    """Boxcar-average blocks of ``factor`` complex64 samples read from the
    binary stream ``f`` (anything with ``readinto``), ``chunk_samples`` at
    a time into one reused buffer; at most ``max_samples`` are read.  A
    last partial block is dropped.  Returns complex128."""
    chunk = max(factor, chunk_samples // factor * factor)
    buf = bytearray(chunk * 8)
    mv = memoryview(buf)
    out: List[np.ndarray] = []
    total = 0
    while total < max_samples:
        want = min(chunk, (max_samples - total) // factor * factor) * 8
        if want <= 0:
            break
        got = 0
        while got < want:
            n = f.readinto(mv[got:want])
            if not n:
                break
            got += n
        nblk = got // 8 // factor
        if nblk:
            x = np.frombuffer(buf, dtype="<c8", count=nblk * factor)
            out.append(x.reshape(nblk, factor).mean(axis=1, dtype=np.complex128))
            total += nblk * factor
        if got < want:
            break
    return np.concatenate(out) if out else np.zeros(0, np.complex128)


def _parabola(logp: np.ndarray, i: int) -> float:
    if i <= 0 or i >= len(logp) - 1:
        return 0.0
    a, b, c = logp[i - 1], logp[i], logp[i + 1]
    den = a - 2 * b + c
    return float(0.5 * (a - c) / den) if den != 0 else 0.0


class _Spec:
    """The frequency grid and masks for one window length (cached)."""

    def __init__(self, n: int, rate: float):
        self.win = np.hanning(n)
        self.fr = np.fft.fftshift(np.fft.fftfreq(NFFT, 1 / rate))
        self.df = float(self.fr[1] - self.fr[0])
        self.search = np.where(np.abs(self.fr) <= SEARCH_HZ)[0]
        af = np.abs(self.fr)
        self.noise = (af > NOISE_HZ[0]) & (af < NOISE_HZ[1])


_SPECS: Dict[Tuple[int, float], _Spec] = {}


def measure_window(y: np.ndarray, rate: float = OUT_RATE) -> dict:
    """The carrier in one window of decimated IQ: ``hz`` (peak, refined),
    ``snr`` (dB over the median noise), and ``hz2``/``snr2`` when a second
    peak is resolved within the search window (else None)."""
    key = (len(y), rate)
    sp = _SPECS.get(key)
    if sp is None:
        sp = _SPECS[key] = _Spec(len(y), rate)
    F = np.fft.fftshift(np.fft.fft(y * sp.win, NFFT))
    P = (F.real ** 2 + F.imag ** 2) + 1e-300
    noise = float(np.median(P[sp.noise])) if sp.noise.any() else 0.0
    idx = sp.search
    i = int(idx[np.argmax(P[idx])])
    logp = np.log(P)
    hz = float(sp.fr[i] + _parabola(logp, i) * sp.df)
    snr = float(10 * np.log10(P[i] / noise)) if noise > 0 else None
    out = {"hz": hz, "snr": snr, "hz2": None, "snr2": None}
    # a second, resolved peak: a local maximum away from the first, strong
    # enough, with a real dip between them
    lo, hi = idx[0], idx[-1]
    seg = P[lo:hi + 1]
    loc = np.where((seg[1:-1] >= seg[:-2]) & (seg[1:-1] > seg[2:]))[0] + 1 + lo
    loc = loc[np.abs(sp.fr[loc] - sp.fr[i]) >= TWO_CARRIER_SEP_HZ]
    if len(loc):
        j = int(loc[np.argmax(P[loc])])
        a, b = sorted((i, j))
        dip = float(P[a:b + 1].min())
        if (P[j] >= P[i] * 10 ** (-TWO_CARRIER_DB / 10)
                and dip <= P[j] * 10 ** (-TWO_CARRIER_DIP_DB / 10)
                and noise > 0 and 10 * np.log10(P[j] / noise) >= MIN_SNR_DB):
            out["hz2"] = float(sp.fr[j] + _parabola(logp, j) * sp.df)
            out["snr2"] = float(10 * np.log10(P[j] / noise))
    return out


def quality(m: dict, gaps: bool) -> str:
    """'weak' (SNR under MIN_SNR_DB), 'edge' (peak near the edge of the
    search window: the carrier may be beyond it), 'gaps' (the file had
    gaps), else 'ok'.  Only 'ok' and 'gaps' minutes are used."""
    if m.get("snr") is None or m["snr"] < MIN_SNR_DB:
        return "weak"
    if abs(m["hz"]) >= EDGE_FRACTION * SEARCH_HZ:
        return "edge"
    return "gaps" if gaps else "ok"


def measure_decimated(y: np.ndarray, rate: float, t0: float, offset_hz: float = 0.0,
                      gaps: bool = False) -> List[dict]:
    """One row per whole minute of ``y`` (decimated IQ starting at epoch
    ``t0``): ``t``, ``hz`` (from nominal: the peak plus ``offset_hz``),
    ``snr``, ``q``, ``hz2``, ``snr2``."""
    n = int(round(60 * rate))
    rows = []
    for m in range(len(y) // n):
        r = measure_window(y[m * n:(m + 1) * n], rate)
        q = quality(r, gaps)
        rows.append({"t": int(round(t0 + 60 * m)), "hz": r["hz"] + offset_hz,
                     "snr": r["snr"], "q": q,
                     "hz2": None if r["hz2"] is None else r["hz2"] + offset_hz,
                     "snr2": r["snr2"]})
    return rows


def read_sidecar(json_path: str) -> dict:
    with open(json_path, "rb") as f:
        meta = json.loads(f.read(1 << 20))
    if not isinstance(meta, dict):
        raise ValueError("sidecar is not an object")
    return meta


def nominal_offset_hz(meta: dict, mhz: float) -> float:
    """What 0 Hz of the file is, Hz from WWV's nominal: the channel's
    ``frequency_hz`` minus ``mhz`` MHz (0 when the json has none)."""
    try:
        return float(meta["frequency_hz"]) - mhz * 1e6
    except (KeyError, TypeError, ValueError):
        return 0.0


def measure_file(bin_path: str, meta: dict, mhz: float, zstd: str = "zstd") -> dict:
    """Stream ``bin_path`` through ``zstd -dc``, decimate it to 100 Hz and
    measure each whole minute.  Returns ``{"rows": [...], "cpu_s":
    float}``; raises on a bad file."""
    if meta.get("dtype", "complex64") != "complex64" or meta.get("byte_order", "little") != "little":
        raise ValueError(f"unsupported sample format {meta.get('dtype')}/{meta.get('byte_order')}")
    fs = float(meta["sample_rate"])
    factor = int(round(fs / OUT_RATE))
    if factor < 1 or abs(fs / factor - OUT_RATE) > 1e-9:
        raise ValueError(f"sample rate {fs} is not a multiple of {OUT_RATE} Hz")
    t0 = float(meta["minute_boundary"])
    dur = float(meta.get("file_duration_sec") or FILE_S)
    max_samples = int(fs * (dur + 60))          # never read past what a file can hold
    gaps = float(meta.get("completeness_pct", 100.0) or 0.0) < GAPS_PCT
    c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    tt0 = time.thread_time()
    proc = subprocess.Popen([zstd, "-dcq", "--", bin_path], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    try:
        y = decimate_stream(proc.stdout, factor, max_samples)
    finally:
        proc.stdout.close()
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
    capped = len(y) * factor >= max_samples - factor   # we stopped reading: SIGPIPE is fine
    if rc != 0 and not capped:
        raise ValueError(f"zstd -dc exited {rc}")
    rows = measure_decimated(y, OUT_RATE, t0, nominal_offset_hz(meta, mhz), gaps)
    c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (time.thread_time() - tt0) + (c1.ru_utime - c0.ru_utime) + (c1.ru_stime - c0.ru_stime)
    return {"rows": rows, "cpu_s": cpu}


# ---- the cache ---------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files (
    channel TEXT NOT NULL, epoch INTEGER NOT NULL, size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL, completeness REAL, n_minutes INTEGER NOT NULL,
    error TEXT, cpu_s REAL, measured_at REAL NOT NULL,
    PRIMARY KEY (channel, epoch));
CREATE TABLE IF NOT EXISTS minutes (
    channel TEXT NOT NULL, t INTEGER NOT NULL, hz REAL NOT NULL, snr REAL,
    q TEXT NOT NULL, hz2 REAL, snr2 REAL, file_epoch INTEGER NOT NULL,
    PRIMARY KEY (channel, t));
"""


class CarrierStore:
    """Per-minute WWV carrier results in SQLite: ``<cache_dir>/wwv-carrier.sqlite``.
    Falls back to a private in-memory database (logged) when the directory
    cannot be written."""

    def __init__(self, cache_dir: Optional[str]):
        self._lock = threading.Lock()
        self.path = None
        self._keep = None
        if cache_dir:
            try:
                os.makedirs(cache_dir, exist_ok=True)
                self.path = os.path.join(cache_dir, CACHE_FILE)
                self._init(self._connect())
            except (OSError, sqlite3.Error) as exc:
                logger.warning("carrier browser: WWV cache %s unusable (%s); "
                               "keeping results in memory only", cache_dir, exc)
                self.path = None
        if self.path is None:
            self._uri = f"file:wwvcarrier{id(self)}?mode=memory&cache=shared"
            self._keep = sqlite3.connect(self._uri, uri=True, check_same_thread=False)
            self._init(self._keep)

    def _connect(self) -> sqlite3.Connection:
        if self.path is None:
            return sqlite3.connect(self._uri, uri=True, timeout=10)
        return sqlite3.connect(self.path, timeout=10)

    def _init(self, con: sqlite3.Connection) -> None:
        with self._lock:
            try:
                if self.path is not None:
                    con.execute("PRAGMA journal_mode=WAL")
                con.executescript(_DDL)
                row = con.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
                if row is None or row[0] != str(SCHEMA):
                    con.executescript("DELETE FROM files; DELETE FROM minutes;")
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
                con.commit()
            finally:
                if con is not self._keep:
                    con.close()

    def known(self, channel: str, epochs: Sequence[int]) -> Dict[int, Tuple[int, int]]:
        """{epoch: (size, mtime_ns)} of the files of ``channel`` measured."""
        if not epochs:
            return {}
        con = self._connect()
        try:
            out = {}
            ep = list(epochs)
            for k in range(0, len(ep), 500):
                part = ep[k:k + 500]
                q = (f"SELECT epoch, size, mtime_ns FROM files WHERE channel=? "
                     f"AND epoch IN ({','.join('?' * len(part))})")
                for e, s, m in con.execute(q, (channel, *part)):
                    out[int(e)] = (int(s), int(m))
            return out
        finally:
            con.close()

    def put(self, channel: str, epoch: int, size: int, mtime_ns: int, completeness,
            rows: List[dict], error: Optional[str], cpu_s: Optional[float]) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute("DELETE FROM minutes WHERE channel=? AND file_epoch=?", (channel, epoch))
                con.executemany(
                    "INSERT OR REPLACE INTO minutes VALUES (?,?,?,?,?,?,?,?)",
                    [(channel, r["t"], r["hz"], r["snr"], r["q"], r["hz2"], r["snr2"], epoch)
                     for r in rows])
                con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                            (channel, epoch, size, mtime_ns, completeness, len(rows), error,
                             cpu_s, time.time()))
                con.commit()
            finally:
                con.close()

    def minutes(self, channel: str, t0: float, t1: float) -> List[tuple]:
        con = self._connect()
        try:
            return con.execute("SELECT t, hz, snr, q, hz2, snr2 FROM minutes WHERE channel=? "
                               "AND t >= ? AND t < ? ORDER BY t",
                               (channel, int(t0), int(np.ceil(t1)))).fetchall()
        finally:
            con.close()

    def channels_with_rows(self, names: Sequence[str]) -> List[str]:
        con = self._connect()
        try:
            return [n for n in names
                    if con.execute("SELECT 1 FROM minutes WHERE channel=? LIMIT 1", (n,)).fetchone()]
        finally:
            con.close()

    def prune(self, before: float) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute("DELETE FROM minutes WHERE t < ?", (int(before),))
                con.execute("DELETE FROM files WHERE epoch < ?", (int(before),))
                con.commit()
            finally:
                con.close()


# ---- the source --------------------------------------------------------------

class RawBufferWwv(WwvSource):
    """WWV's Doppler measured from hf-timestd's raw IQ buffers (see the
    module docstring).  ``series`` answers as ``ww0wwv.WwvSource.series``,
    plus ``limit_hz`` (the search window), ``reliable``, ``blend``
    ('WWV+WWVH' on shared frequencies, else 'WWV'), ``pending`` (files
    queued, not yet measured) and per point ``q``, ``ok``, ``two`` and,
    for two carriers, ``hz2``/``ppb2``/``snr2``.

    ``fallback`` (e.g. ``TimestdWwv``) answers for a frequency whose
    channel has no raw-buffer directory under any root.  ``prefill``: keep
    the current UTC day measured in the background; ``background`` False
    (tests) leaves queued files to ``drain()``."""

    name = "raw IQ (hf-timestd raw_buffer)"
    limit_hz = SEARCH_HZ
    reliable = True

    def __init__(self, roots: Sequence[str] = DEFAULT_ROOTS, cache_dir: Optional[str] = None,
                 fallback=None, prefill: bool = False, duty: float = 0.25,
                 zstd: Optional[str] = None, keep_days: int = KEEP_DAYS,
                 sync_files: int = SYNC_FILES, background: bool = True):
        self.roots = [r for r in roots if r]
        self.store = CarrierStore(cache_dir)
        self.cache_dir = cache_dir
        self.fallback = fallback
        self.zstd = zstd or shutil.which("zstd") or "/usr/bin/zstd"
        self.duty = min(1.0, max(0.01, duty))
        self.keep_days = keep_days
        self.sync_files = sync_files
        self.flight = SingleFlight()
        self._qlock = threading.Lock()
        self._queue: "collections.OrderedDict" = collections.OrderedDict()
        self._wake = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._bad: set = set()
        self.measured = 0          # files measured (tests, logs)
        self.prefill = prefill
        self.background = background or prefill
        self._last_prune = 0.0
        if prefill:
            self._start()

    # -- files -----------------------------------------------------------------

    def channel_names(self, mhz: float) -> List[str]:
        khz = int(round(mhz * 1000))
        return [f"WWV_{khz}", f"SHARED_{khz}"]

    def channel_for(self, mhz: float) -> Optional[str]:
        """The raw-buffer channel for ``mhz``: the first name with a
        directory under some root, else one the cache already has rows for."""
        names = self.channel_names(mhz)
        for n in names:
            if any(os.path.isdir(os.path.join(r, n)) for r in self.roots):
                return n
        have = self.store.channels_with_rows(names)
        return have[0] if have else None

    def files(self, channel: str, t0: float, t1: float) -> Dict[int, dict]:
        """{epoch: {bin, json, size, mtime_ns}} of the complete files (both
        .bin.zst and .json present) of ``channel`` that overlap [t0, t1);
        the first root that has a file wins."""
        out: Dict[int, dict] = {}
        days = [time.strftime("%Y%m%d", time.gmtime(86400 * k))
                for k in range(int((t0 - FILE_S) // 86400), int(t1 // 86400) + 1)]
        for root in self.roots:
            for day in days:
                dd = os.path.join(root, channel, day)
                try:
                    names = os.listdir(dd)
                except OSError:
                    continue
                for fn in names:
                    m = NAME_RE.match(fn)
                    if not m:
                        continue
                    e = int(m.group(1))
                    if e in out or e + FILE_S <= t0 or e >= t1:
                        continue
                    b = os.path.join(dd, fn)
                    j = b[:-len(".bin.zst")] + ".json"
                    try:
                        st = os.stat(b)
                        if not os.path.isfile(j):
                            continue
                    except OSError:
                        continue
                    out[e] = {"bin": b, "json": j, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        return out

    def _measure(self, channel: str, mhz: float, epoch: int, f: dict):
        """Measure one file into the cache (single-flight per file)."""
        key = (channel, epoch, f["size"], f["mtime_ns"])

        def run():
            have = self.store.known(channel, [epoch]).get(epoch)
            if have == (f["size"], f["mtime_ns"]):
                return
            rows, err, cpu, comp = [], None, None, None
            try:
                meta = read_sidecar(f["json"])
                comp = meta.get("completeness_pct")
                r = measure_file(f["bin"], meta, mhz, self.zstd)
                rows, cpu = r["rows"], r["cpu_s"]
            except Exception as exc:                  # noqa: BLE001 -- a bad file
                err = f"{type(exc).__name__}: {exc}"[:300]
                if f["bin"] not in self._bad:
                    self._bad.add(f["bin"])
                    logger.warning("carrier browser: cannot measure WWV in %s: %s", f["bin"], err)
            self.store.put(channel, epoch, f["size"], f["mtime_ns"], comp, rows, err, cpu)
            self.measured += 1
            return cpu
        return self.flight.do(key, run)

    # -- background ----------------------------------------------------------------

    def _start(self) -> None:
        with self._qlock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="wwv-carrier", daemon=True)
                self._worker.start()

    def enqueue(self, channel: str, mhz: float, todo: Dict[int, dict]) -> None:
        with self._qlock:
            for e in sorted(todo, reverse=True):          # newest first
                self._queue[(channel, e)] = (mhz, todo[e])
        if self.background:
            self._start()
            self._wake.set()

    def pending(self, channel: str, t0: float = float("-inf"), t1: float = float("inf")) -> int:
        """Files of ``channel`` overlapping [t0, t1) queued, not yet measured."""
        with self._qlock:
            return sum(1 for (c, e) in self._queue if c == channel and t0 - FILE_S < e < t1)

    def drain(self, limit: Optional[int] = None) -> int:
        """Measure queued files now, in this thread (tests; the worker runs
        the same steps)."""
        n = 0
        while limit is None or n < limit:
            with self._qlock:
                if not self._queue:
                    break
                (ch, e), (mhz, f) = self._queue.popitem(last=False)
            self._measure(ch, mhz, e, f)
            n += 1
        return n

    def _run(self) -> None:
        while True:
            try:
                with self._qlock:
                    item = self._queue.popitem(last=False) if self._queue else None
                if item is None:
                    if self.prefill:
                        self._prefill_today()
                        with self._qlock:
                            empty = not self._queue
                        if empty:
                            self._wake.wait(60)
                            self._wake.clear()
                    else:
                        self._wake.wait(60)
                        self._wake.clear()
                    continue
                (ch, e), (mhz, f) = item
                t = time.monotonic()
                self._measure(ch, mhz, e, f)
                busy = time.monotonic() - t
                time.sleep(busy * (1.0 / self.duty - 1.0))      # cap the duty cycle
            except Exception:                                   # noqa: BLE001 -- keep going
                logger.warning("carrier browser: WWV worker", exc_info=True)
                time.sleep(30)

    def _prefill_today(self) -> None:
        now = time.time()
        if now - self._last_prune > 86400:
            self._last_prune = now
            self.store.prune(now - self.keep_days * 86400)
        day0 = now // 86400 * 86400
        for mhz in sorted({m for _b, m in BAND_PAIRS}):
            ch = self.channel_for(mhz)
            if ch is None:
                continue
            todo = self._missing(ch, self.files(ch, day0, now))
            if todo:
                with self._qlock:
                    for e in sorted(todo, reverse=True):
                        self._queue.setdefault((ch, e), (mhz, todo[e]))

    def _missing(self, channel: str, fs: Dict[int, dict]) -> Dict[int, dict]:
        have = self.store.known(channel, list(fs))
        return {e: f for e, f in fs.items() if have.get(e) != (f["size"], f["mtime_ns"])}

    # -- the series ----------------------------------------------------------------

    def series(self, mhz: float, t0: float, t1: float) -> dict:
        ch = self.channel_for(mhz)
        if ch is None:
            if self.fallback is not None:
                return self.fallback.series(mhz, t0, t1)
            return self._none(f"no raw buffer for WWV {mhz:g} MHz "
                              f"({' or '.join(self.channel_names(mhz))}) under "
                              f"{' or '.join(self.roots) or 'no root'}", mhz)
        todo = self._missing(ch, self.files(ch, t0, t1))
        if todo and len(todo) <= self.sync_files:
            for e in sorted(todo):
                self._measure(ch, mhz, e, todo[e])
        elif todo:
            self.enqueue(ch, mhz, todo)
        nominal = mhz * 1e6
        pts = []
        for t, hz, snr, q, hz2, snr2 in self.store.minutes(ch, t0, t1):
            p = {"t": int(t), "hz": round(hz, 6), "ppb": round(hz / nominal * 1e9, 4),
                 "snr": None if snr is None else round(snr, 2), "q": q,
                 "ok": q in ("ok", "gaps"), "two": hz2 is not None}
            if hz2 is not None:
                p.update({"hz2": round(hz2, 6), "ppb2": round(hz2 / nominal * 1e9, 4),
                          "snr2": None if snr2 is None else round(snr2, 2)})
            pts.append(p)
        pend = self.pending(ch, t0, t1)
        out = self._none("", mhz)
        out.update({"channel": ch, "points": pts, "pending": pend,
                    "available": any(p["ok"] for p in pts)})
        if not out["available"]:
            out["reason"] = (f"measuring WWV {mhz:g} MHz ({ch}): {pend} ten-minute files queued"
                             if pend else
                             f"no usable WWV {mhz:g} MHz carrier in {ch}'s raw buffer "
                             f"in this period")
        return out


def roots_from(value: Optional[str]) -> List[str]:
    """A colon-separated list of raw-buffer roots."""
    if value is None:
        return list(DEFAULT_ROOTS)
    return [r for r in value.split(":") if r]


def cache_dir_from(value: Optional[str], env: Optional[Dict[str, str]] = None) -> str:
    """--wwv-cache-dir, else systemd's $CACHE_DIRECTORY (its first entry),
    else /var/cache/wspr-carrier-browser."""
    if value:
        return value
    env = os.environ if env is None else env
    cd = (env.get("CACHE_DIRECTORY") or "").split(":")[0]
    return cd or DEFAULT_CACHE_DIR


__all__ = ["RawBufferWwv", "CarrierStore", "SingleFlight", "decimate_stream", "measure_window",
           "measure_decimated", "measure_file", "roots_from", "cache_dir_from"]
