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

import contextlib
import json
import logging
import os
import re
import resource
import shutil
import sqlite3
import subprocess
import threading
import math
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .ww0wwv import BAND_PAIRS, WwvSource

logger = logging.getLogger(__name__)

DEFAULT_ROOTS = ("/var/lib/timestd/raw_buffer", "/dev/shm/timestd/raw_buffer")
DEFAULT_CACHE_DIR = "/var/cache/wspr-carrier-browser"
CACHE_FILE = "wwv-carrier.sqlite"
SCHEMA = 2
# hf-timestd's raw sample formats (hf_timestd.core.iq_codec): complex64, or
# binary16 components scaled by F16_SCALE (archive_dtype = "f16", its default).
DTYPE_C64 = "complex64"
DTYPE_F16 = "complex-f16"
F16_SCALE = 4096.0

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
# The unit's MemoryMax=256M counts the zstd children: at most two
# measurements at once (request threads and the background thread
# together), each zstd held to 64 MB of decompression memory.
MAX_CONCURRENT_MEASUREMENTS = 2
_MEASURE_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_MEASUREMENTS)
ZSTD_MEMORY = "--memory=64MB"
READ_TIMEOUT_S = 120.0      # zstd is killed after this
FOLLOW_TIMEOUT_S = 150.0    # a request waiting on another's measurement gives up
RETRY_S = 600.0             # a transient failure is retried after 10 minutes
MAX_FS, MAX_DUR_S = 48000.0, 1200.0     # the read cap trusts the sidecar no further
PRUNE_EVERY_S = 3600.0
PENDING = "pending"         # _measure's answer when it gave up waiting


class CorruptFile(ValueError):
    """The file's data is bad: cached, never retried (until it changes)."""


class TransientError(Exception):
    """Could not measure now (killed, timed out, unreadable, out of memory):
    retried after RETRY_S."""


class FlightTimeout(Exception):
    """A single-flight follower stopped waiting."""


def _now() -> float:
    return time.time()


def _finite(v) -> bool:
    return v is not None and math.isfinite(v)


# ---- single flight -----------------------------------------------------------

class SingleFlight:
    """Concurrent calls with one key share one computation: the first runs
    it, the rest wait for its result (or its exception)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running: Dict[object, list] = {}

    def do(self, key, fn: Callable, timeout: Optional[float] = None):
        """Run ``fn`` once per key at a time; a follower waits at most
        ``timeout`` seconds (FlightTimeout), the leader as long as it takes."""
        with self._lock:
            slot = self._running.get(key)
            leader = slot is None
            if leader:
                slot = self._running[key] = [threading.Event(), None, None]
        if not leader:
            if not slot[0].wait(timeout):
                raise FlightTimeout(key)
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
                    chunk_samples: int = CHUNK_SAMPLES,
                    fmt: str = DTYPE_C64) -> np.ndarray:
    """Boxcar-average blocks of ``factor`` complex samples read from the
    binary stream ``f`` (anything with ``readinto``), ``chunk_samples`` at
    a time into one reused buffer; at most ``max_samples`` are read.  A
    last partial block is dropped.  Returns complex128.

    ``fmt`` is the sidecar's dtype: ``complex64``, or ``complex-f16``
    (hf-timestd's default archive: interleaved little-endian binary16 of
    each component times F16_SCALE)."""
    width = 4 if fmt == DTYPE_F16 else 8
    chunk = max(factor, chunk_samples // factor * factor)
    buf = bytearray(chunk * width)
    mv = memoryview(buf)
    out: List[np.ndarray] = []
    total = 0
    while total < max_samples:
        want = min(chunk, (max_samples - total) // factor * factor) * width
        if want <= 0:
            break
        got = 0
        while got < want:
            n = f.readinto(mv[got:want])
            if not n:
                break
            got += n
        nblk = got // width // factor
        if nblk:
            if fmt == DTYPE_F16:
                comp = np.frombuffer(buf, dtype="<f2", count=2 * nblk * factor)
                x = (comp.astype(np.float32) / np.float32(F16_SCALE)).view(np.complex64)
            else:
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
    if not _finite(snr):
        snr = None
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
            hz2 = float(sp.fr[j] + _parabola(logp, j) * sp.df)
            snr2 = float(10 * np.log10(P[j] / noise))
            if _finite(hz2) and _finite(snr2):
                out["hz2"], out["snr2"] = hz2, snr2
    return out


def quality(m: dict, gaps: bool) -> str:
    """'weak' (SNR under MIN_SNR_DB), 'edge' (peak near the edge of the
    search window: the carrier may be beyond it), 'gaps' (the file had
    gaps), else 'ok'.  Only 'ok' and 'gaps' minutes are used."""
    if not _finite(m.get("snr")) or not _finite(m.get("hz")) or m["snr"] < MIN_SNR_DB:
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
        if not _finite(r["hz"]):
            continue                                   # never written
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


_CORRUPT = ("unsupported format", "premature end", "corrupt", "checksum",
            "unknown frame descriptor", "not in zstd format", "truncated")


def classify_zstd(rc: int, err: str, killed: bool) -> str:
    """'corrupt' when zstd says the data is bad; 'transient' when it was
    killed (watchdog, OOM), ran out of its memory limit, or could not read."""
    if killed or rc < 0:
        return "transient"
    e = (err or "").lower()
    if "memory" in e:
        return "transient"
    return "corrupt" if any(k in e for k in _CORRUPT) else "transient"


def read_cap(fs: float, dur: float) -> int:
    """Samples read at most from one file: its duration plus a minute,
    with the sidecar's rate and duration clamped (48 kHz, 1200 s)."""
    fs = min(max(float(fs), 0.0), MAX_FS)
    dur = min(max(float(dur), 0.0), MAX_DUR_S)
    return int(fs * (dur + 60))


def measure_file(bin_path: str, meta: dict, mhz: float, zstd: str = "zstd") -> dict:
    """Stream ``bin_path`` through ``zstd -dc``, decimate it to 100 Hz and
    measure each whole minute.  Returns ``{"rows": [...], "cpu_s":
    float}``.  Raises CorruptFile for bad data (or a malformed sidecar),
    TransientError for anything worth retrying.  At most
    MAX_CONCURRENT_MEASUREMENTS run at once, process-wide."""
    try:
        fmt = meta.get("dtype", DTYPE_C64)
        if (fmt not in (DTYPE_C64, DTYPE_F16)
                or meta.get("byte_order", "little") != "little"):
            raise ValueError(f"unsupported sample format {meta.get('dtype')}/"
                             f"{meta.get('byte_order')}")
        fs = float(meta["sample_rate"])
        factor = int(round(fs / OUT_RATE))
        if not math.isfinite(fs) or factor < 1 or abs(fs / factor - OUT_RATE) > 1e-9:
            raise ValueError(f"sample rate {fs} is not a multiple of {OUT_RATE} Hz")
        t0 = float(meta["minute_boundary"])
        max_samples = read_cap(fs, float(meta.get("file_duration_sec") or FILE_S))
        gaps = float(meta.get("completeness_pct", 100.0) or 0.0) < GAPS_PCT
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CorruptFile(f"malformed sidecar: {exc}") from None
    with _MEASURE_SLOTS:
        c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        tt0 = time.thread_time()
        try:
            proc = subprocess.Popen([zstd, "-dcq", ZSTD_MEMORY, "--", bin_path],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            raise TransientError(f"cannot run {zstd}: {exc}") from None
        killed = []
        dog = threading.Timer(READ_TIMEOUT_S, lambda: (killed.append(1), proc.kill()))
        dog.daemon = True
        dog.start()
        try:
            y = decimate_stream(proc.stdout, factor, max_samples, fmt=fmt)
        except OSError as exc:
            proc.kill()
            raise TransientError(f"reading zstd's output: {exc}") from None
        finally:
            dog.cancel()
            proc.stdout.close()
            try:
                err = proc.stderr.read(4096).decode("utf-8", "replace").strip()
            except OSError:
                err = ""
            proc.stderr.close()
            try:
                rc = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
        if killed:
            raise TransientError(f"zstd killed after {READ_TIMEOUT_S:g} s")
        capped = len(y) * factor >= max_samples - factor   # we stopped reading: SIGPIPE is fine
        if rc != 0 and not capped:
            msg = f"zstd -dc exited {rc}: {err or 'no message'}"
            if classify_zstd(rc, err, False) == "corrupt":
                raise CorruptFile(msg)
            raise TransientError(msg)
        if len(y) < int(60 * OUT_RATE):
            raise CorruptFile(f"short payload: {len(y) * factor} samples, not one minute")
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
    error TEXT, cpu_s REAL, measured_at REAL NOT NULL, retry_after REAL,
    PRIMARY KEY (channel, epoch));
CREATE TABLE IF NOT EXISTS minutes (
    channel TEXT NOT NULL, t INTEGER NOT NULL, hz REAL NOT NULL, snr REAL,
    q TEXT NOT NULL, hz2 REAL, snr2 REAL, file_epoch INTEGER NOT NULL,
    PRIMARY KEY (channel, t));
"""


class CarrierStore:
    """Per-minute WWV carrier results in SQLite: ``<cache_dir>/wwv-carrier.sqlite``.
    Falls back to one private in-memory connection (logged) when the
    directory cannot be written.  Writes are serialized by one lock; so is
    every use of the in-memory connection."""

    def __init__(self, cache_dir: Optional[str]):
        self._lock = threading.RLock()
        self.path = None
        self._mem: Optional[sqlite3.Connection] = None
        if cache_dir:
            try:
                os.makedirs(cache_dir, exist_ok=True)
                self.path = os.path.join(cache_dir, CACHE_FILE)
                self._init()
            except (OSError, sqlite3.Error) as exc:
                logger.warning("carrier browser: WWV cache %s unusable (%s); "
                               "keeping results in memory only", cache_dir, exc)
                self.path = None
        if self.path is None:
            self._mem = sqlite3.connect(":memory:", check_same_thread=False)
            self._init()

    @contextlib.contextmanager
    def _use(self, write: bool = False):
        if self._mem is not None:
            with self._lock:
                yield self._mem
            return
        with (self._lock if write else contextlib.nullcontext()):
            con = sqlite3.connect(self.path, timeout=10)
            try:
                yield con
            finally:
                con.close()

    def _init(self) -> None:
        with self._use(write=True) as con:
            if self.path is not None:
                con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_DDL)
            row = con.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
            if row is None or row[0] != str(SCHEMA):
                con.executescript("DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS minutes;")
                con.executescript(_DDL)
                con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
            # Files refused before complex-f16 was readable get measured now.
            con.execute("DELETE FROM files WHERE error LIKE '%unsupported sample format%'")
            con.commit()

    def known(self, channel: str, epochs: Sequence[int]) -> Dict[int, Tuple[int, int]]:
        """{epoch: (size, mtime_ns)} of the files of ``channel`` measured
        (a transient failure counts until its retry time)."""
        if not epochs:
            return {}
        out = {}
        ep = list(epochs)
        now = _now()
        with self._use() as con:
            for k in range(0, len(ep), 500):
                part = ep[k:k + 500]
                q = (f"SELECT epoch, size, mtime_ns FROM files WHERE channel=? "
                     f"AND (retry_after IS NULL OR retry_after > ?) "
                     f"AND epoch IN ({','.join('?' * len(part))})")
                for e, sz, m in con.execute(q, (channel, now, *part)):
                    out[int(e)] = (int(sz), int(m))
        return out

    def put(self, channel: str, epoch: int, size: int, mtime_ns: int, completeness,
            rows: List[dict], error: Optional[str], cpu_s: Optional[float],
            retry_after: Optional[float] = None) -> None:
        with self._use(write=True) as con:
            con.execute("DELETE FROM minutes WHERE channel=? AND file_epoch=?", (channel, epoch))
            con.executemany(
                "INSERT OR REPLACE INTO minutes VALUES (?,?,?,?,?,?,?,?)",
                [(channel, r["t"], r["hz"], r["snr"], r["q"], r["hz2"], r["snr2"], epoch)
                 for r in rows if _finite(r.get("hz"))])
            con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (channel, epoch, size, mtime_ns, completeness, len(rows), error,
                         cpu_s, _now(), retry_after))
            con.commit()

    def minutes(self, channel: str, t0: float, t1: float) -> List[tuple]:
        with self._use() as con:
            return con.execute("SELECT t, hz, snr, q, hz2, snr2 FROM minutes WHERE channel=? "
                               "AND t >= ? AND t < ? ORDER BY t",
                               (channel, int(t0), int(np.ceil(t1)))).fetchall()

    def channels_with_rows(self, names: Sequence[str]) -> List[str]:
        with self._use() as con:
            return [n for n in names
                    if con.execute("SELECT 1 FROM minutes WHERE channel=? LIMIT 1", (n,)).fetchone()]

    def prune(self, before: float) -> None:
        with self._use(write=True) as con:
            con.execute("DELETE FROM minutes WHERE t < ?", (int(before),))
            con.execute("DELETE FROM files WHERE epoch < ?", (int(before),))
            con.commit()


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
        self._queue: Dict[Tuple[str, int], tuple] = {}
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
        try:
            have = self.store.channels_with_rows(names)
        except sqlite3.Error as exc:
            self._db_warn(exc)
            return None
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

    def _warn_once(self, key, msg: str, *args) -> None:
        if key in self._bad:
            return
        if len(self._bad) > 10000:
            self._bad.clear()
        self._bad.add(key)
        logger.warning(msg, *args)

    def _db_warn(self, exc: Exception) -> None:
        self._warn_once(("db", str(exc)), "carrier browser: WWV cache %s: %s",
                        self.store.path or "(memory)", exc)

    def _measure(self, channel: str, mhz: float, epoch: int, f: dict):
        """Measure one file into the cache (single-flight per file).
        Returns its rows (also when the cache cannot be written), None when
        it was already measured or failed, PENDING when another thread is
        measuring it and did not finish within FOLLOW_TIMEOUT_S."""
        key = (channel, epoch, f["size"], f["mtime_ns"])

        def run():
            try:
                have = self.store.known(channel, [epoch]).get(epoch)
            except sqlite3.Error as exc:
                self._db_warn(exc)
                have = None
            if have == (f["size"], f["mtime_ns"]):
                return None
            rows, err, cpu, comp, retry = [], None, None, None, None
            try:
                try:
                    meta = read_sidecar(f["json"])
                except OSError as exc:
                    raise TransientError(f"cannot read {f['json']}: {exc}") from None
                except ValueError as exc:
                    raise CorruptFile(f"bad sidecar: {exc}") from None
                comp = meta.get("completeness_pct")
                r = measure_file(f["bin"], meta, mhz, self.zstd)
                rows, cpu = r["rows"], r["cpu_s"]
            except CorruptFile as exc:
                err = f"corrupt: {exc}"[:300]
            except Exception as exc:                  # noqa: BLE001 -- retried later
                err = f"transient: {type(exc).__name__}: {exc}"[:300]
                retry = _now() + RETRY_S
            if err:
                self._warn_once((f["bin"], err), "carrier browser: cannot measure WWV in %s: %s%s",
                                f["bin"], err, " (retried in 10 min)" if retry else "")
            if not isinstance(comp, (int, float)) or not math.isfinite(comp):
                comp = None
            try:
                self.store.put(channel, epoch, f["size"], f["mtime_ns"], comp, rows, err, cpu,
                               retry)
            except sqlite3.Error as exc:
                self._db_warn(exc)
            self.measured += 1
            return rows
        try:
            return self.flight.do(key, run, timeout=FOLLOW_TIMEOUT_S)
        except FlightTimeout:
            return PENDING

    # -- background ----------------------------------------------------------------

    def _start(self) -> None:
        with self._qlock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="wwv-carrier", daemon=True)
                self._worker.start()

    def enqueue(self, channel: str, mhz: float, todo: Dict[int, dict]) -> None:
        with self._qlock:
            for e in todo:
                self._queue[(channel, e)] = (mhz, todo[e])
        if self.background:
            self._start()
            self._wake.set()

    def pending(self, channel: str, t0: float = float("-inf"), t1: float = float("inf")) -> int:
        """Files of ``channel`` overlapping [t0, t1) queued, not yet measured."""
        with self._qlock:
            return sum(1 for (c, e) in self._queue if c == channel and t0 - FILE_S < e < t1)

    def _pop(self):
        """The queued file with the latest epoch: today's (prefill) ahead
        of a request's historical ones."""
        with self._qlock:
            if not self._queue:
                return None
            k = max(self._queue, key=lambda ce: (ce[1], ce[0]))
            return k, self._queue.pop(k)

    def drain(self, limit: Optional[int] = None) -> int:
        """Measure queued files now, in this thread (tests; the worker runs
        the same steps)."""
        n = 0
        while limit is None or n < limit:
            item = self._pop()
            if item is None:
                break
            (ch, e), (mhz, f) = item
            self._measure(ch, mhz, e, f)
            n += 1
        return n

    def _run(self) -> None:
        while True:
            try:
                item = self._pop()
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
        self._maybe_prune()
        now = time.time()
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

    def _maybe_prune(self) -> None:
        """Drop cache rows older than keep_days, at most once an hour
        (from the request path too, so it happens without prefill)."""
        now = _now()
        if now - self._last_prune < PRUNE_EVERY_S:
            return
        self._last_prune = now
        try:
            self.store.prune(now - self.keep_days * 86400)
        except sqlite3.Error as exc:
            self._db_warn(exc)

    def _missing(self, channel: str, fs: Dict[int, dict]) -> Dict[int, dict]:
        have = self.store.known(channel, list(fs))
        return {e: f for e, f in fs.items() if have.get(e) != (f["size"], f["mtime_ns"])}

    # -- the series ----------------------------------------------------------------

    def series(self, mhz: float, t0: float, t1: float) -> dict:
        self._maybe_prune()
        ch = self.channel_for(mhz)
        if ch is None:
            if self.fallback is not None:
                return self.fallback.series(mhz, t0, t1)
            return self._none(f"no raw buffer for WWV {mhz:g} MHz "
                              f"({' or '.join(self.channel_names(mhz))}) under "
                              f"{' or '.join(self.roots) or 'no root'}", mhz)
        fs = self.files(ch, t0, t1)
        try:
            have = self.store.known(ch, list(fs))
        except sqlite3.Error as exc:
            self._db_warn(exc)
            out = self._none(f"WWV cache unreadable ({exc}); not measuring", mhz)
            out["channel"] = ch
            return out
        todo = {e: f for e, f in fs.items() if have.get(e) != (f["size"], f["mtime_ns"])}
        fresh: Dict[int, tuple] = {}
        waiting = 0
        if todo and len(todo) <= self.sync_files:
            for e in sorted(todo):
                r = self._measure(ch, mhz, e, todo[e])
                if r is PENDING:
                    waiting += 1
                elif r:                       # kept even if the cache could not take them
                    for row in r:
                        fresh[row["t"]] = (row["t"], row["hz"], row["snr"], row["q"],
                                           row["hz2"], row["snr2"])
        elif todo:
            self.enqueue(ch, mhz, todo)
        dberr = None
        try:
            rows = self.store.minutes(ch, t0, t1)
        except sqlite3.Error as exc:
            self._db_warn(exc)
            rows, dberr = [], str(exc)
        merged = {int(r[0]): r for r in rows}
        merged.update({t: r for t, r in fresh.items() if t0 <= t < t1})
        nominal = mhz * 1e6
        pts = []
        for t, hz, snr, q, hz2, snr2 in (merged[k] for k in sorted(merged)):
            if not _finite(hz):
                continue
            if not _finite(snr):
                snr, q = None, "weak"
            p = {"t": int(t), "hz": round(hz, 6), "ppb": round(hz / nominal * 1e9, 4),
                 "snr": None if snr is None else round(snr, 2), "q": q,
                 "ok": q in ("ok", "gaps"), "two": _finite(hz2)}
            if _finite(hz2):
                p.update({"hz2": round(hz2, 6), "ppb2": round(hz2 / nominal * 1e9, 4),
                          "snr2": round(snr2, 2) if _finite(snr2) else None})
            pts.append(p)
        pend = self.pending(ch, t0, t1) + waiting
        out = self._none("", mhz)
        out.update({"channel": ch, "points": pts, "pending": pend,
                    "available": any(p["ok"] for p in pts)})
        if not out["available"]:
            if dberr:
                out["reason"] = f"WWV cache unreadable ({dberr})"
            elif pend:
                out["reason"] = (f"measuring WWV {mhz:g} MHz ({ch}): {pend} ten-minute "
                                 f"files queued")
            else:
                out["reason"] = (f"no usable WWV {mhz:g} MHz carrier in {ch}'s raw buffer "
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


__all__ = ["RawBufferWwv", "CarrierStore", "SingleFlight", "FlightTimeout", "CorruptFile",
           "TransientError", "classify_zstd", "read_cap", "decimate_stream", "measure_window",
           "measure_decimated", "measure_file", "roots_from", "cache_dir_from"]
