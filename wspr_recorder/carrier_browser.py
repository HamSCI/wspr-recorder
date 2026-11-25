"""A read-only web page for wspr-recorder's carrier archive (carrier_archive.py).

    python -m wspr_recorder.carrier_browser --root DIR [--bind ADDR] [--port 8089]
                                            [--timestd-db PATH]

DIR is the archive: YYYYMMDD/RECEIVER/BAND/HHMM_MODE_CALL_RFHZ.wav, each a
2-channel float32 IQ .wav at 15 Hz with the decode's particulars as JSON in
its LIST/INFO/ICMT chunk (ubersdr's carrier_iq.h).

Main page (the beta's purpose): WW0WWV against WWV.  For a UTC day, one
chart per band pair (30 m / WWV 10 MHz, 20 m / 15, 15 m / 20, 12 m and
10 m / 25): WW0WWV's Doppler in ppb of its nominal frequency and its SNR,
one point per WSPR cycle, against WWV's Doppler from hf-timestd (one a
minute).  Clicking a cycle opens its carrier's spectrogram and Doppler
track beside WWV's for the same minutes.  The physics is in ww0wwv.py.

Second page, "All spots": the archive's dates, cycles and spots, band by
band; a spot's file is plotted in the browser and can be downloaded.

Standard library and numpy.  Ported from wsprdaemon's carrier-browser.py
(WD c1835bf), hardened:
  * read-only: GET only, nothing under DIR (or the hf-timestd database,
    opened mode=ro) is created or written;
  * only DIR is served: /file resolves symlinks and refuses anything whose
    real path is not under DIR's; listings skip symlinks and entries whose
    real path leaves DIR, so a link cannot expose a file's name or comment;
  * binds 127.0.0.1 unless told otherwise (--bind, or CARRIER_BROWSER_BIND);
  * DNS rebinding: a request is refused unless its Host header is an IP
    address literal, a loopback name, this host's name, or a name in
    CARRIER_BROWSER_ALLOWED_HOSTS (comma-separated) -- whatever the bind;
  * text from file names and WAV comments reaches the page's HTML escaped.

API (JSON): /api/compare?date=YYYYMMDD, /api/cycle?date=&hhmm=&band=[&rx=],
/api/dates, /api/cycles?date=YYYYMMDD, /api/spots?date=YYYYMMDD&hhmm=HHMM;
files: /file?path=REL.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import os
import re
import socket
import sqlite3
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import OrderedDict
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import ww0wwv as W
from .carrier_archive import base_call, safe_name

logger = logging.getLogger(__name__)

NAME = re.compile(r"^(\d{4})_([A-Z]\d+)_(.+)_(\d+\.\d+)\.wav$")
_DATE = re.compile(r"\d{8}")
_HHMM = re.compile(r"\d{4}")
_BAND = re.compile(r"[0-9a-z]{1,8}")
# Largest carrier file served: an F30 carrier is 1800 s x 15 Hz x 8 B, about
# 216 KB; anything far bigger is not one of ours.
MAX_FILE_BYTES = 16 * 1024 * 1024
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8089
DEFAULT_TIMESTD_DB = "/var/lib/timestd/phase2/timestd.db"
ENV_BIND = "CARRIER_BROWSER_BIND"
ENV_PORT = "CARRIER_BROWSER_PORT"
ENV_ALLOWED = "CARRIER_BROWSER_ALLOWED_HOSTS"
ENV_TIMESTD_DB = "TIMESTD_DB"
ENV_SINK_DB = "CARRIER_BROWSER_SINK_DB"
SINK_CONFIG = "/etc/hs-uploader/sink.toml"
LEGACY_SINK = "/var/lib/sigmond/sink.db"
STATE_SINK = "/var/lib/hs-uploader/sink.db"
# Requests served at once; more get 503 (the page makes a handful).
MAX_CONCURRENT = 8


def _under(root: str, path: str) -> bool:
    """True iff `path`'s real path is `root` (already real) or inside it."""
    real = os.path.realpath(path)
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def _subdirs(root: str, base: str) -> List[str]:
    """Names of `base`'s subdirectories that are real directories inside
    `root` (no symlinks)."""
    try:
        entries = list(os.scandir(base))
    except OSError:
        return []
    out = []
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False) and _under(root, e.path):
                out.append(e.name)
        except OSError:
            continue
    return sorted(out)


def icmt(path: str) -> Tuple[dict, int]:
    """The JSON in a carrier .wav's LIST/INFO/ICMT chunk, and its sample count; ({}, 0) if unreadable."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return {}, 0
        i, meta, n = 12, {}, 0
        while i + 8 <= len(head):
            cid, size = head[i:i + 4], struct.unpack("<I", head[i + 4:i + 8])[0]
            body = head[i + 8:i + 8 + size]
            if cid == b"LIST" and body[:4] == b"INFO":
                j = 4
                while j + 8 <= len(body):
                    sid, m = body[j:j + 4], struct.unpack("<I", body[j + 4:j + 8])[0]
                    if sid == b"ICMT":
                        meta = json.loads(body[j + 8:j + 8 + m].split(b"\0")[0] or b"{}")
                    j += 8 + m + (m & 1)
            elif cid == b"data":
                n = size // 8
                break
            i += 8 + size + (size & 1)
        return (meta if isinstance(meta, dict) else {}), n
    except (OSError, ValueError, struct.error):
        return {}, 0


def dates(root: str) -> List[str]:
    return sorted((d for d in _subdirs(root, root) if _DATE.fullmatch(d)), reverse=True)


def walk_date(root: str, date: str) -> Iterator[Tuple[str, str, str]]:
    """(receiver, band, filename) for every carrier file of a date: regular
    files only, never through a symlink, never outside root."""
    if not _DATE.fullmatch(date or ""):
        return
    base = os.path.join(root, date)
    if date not in _subdirs(root, root):
        return
    for rx in _subdirs(root, base):
        rxd = os.path.join(base, rx)
        for band in _subdirs(root, rxd):
            bd = os.path.join(rxd, band)
            try:
                entries = list(os.scandir(bd))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.name.endswith(".wav") and e.is_file(follow_symlinks=False):
                        yield rx, band, e.name
                except OSError:
                    continue


def cycles(root: str, date: str) -> List[dict]:
    count: dict = {}
    for rx, band, fn in walk_date(root, date):
        m = NAME.match(fn)
        if m:
            count[m.group(1)] = count.get(m.group(1), 0) + 1
    return [{"hhmm": h, "spots": count[h]} for h in sorted(count, reverse=True)]


def spots(root: str, date: str, hhmm: str) -> List[dict]:
    out = []
    for rx, band, fn in walk_date(root, date):
        m = NAME.match(fn)
        if not m or m.group(1) != hhmm:
            continue
        rel = f"{date}/{rx}/{band}/{fn}"
        meta, n = icmt(os.path.join(root, rel))
        out.append({"receiver": rx, "band": band, "mode": m.group(2), "call": m.group(3), "rf_hz": float(m.group(4)),
                    "path": rel, "samples": n, "meta": meta})
    out.sort(key=lambda s: (s["receiver"], s["rf_hz"]))
    return out


def resolve_file(root: str, rel: str) -> Optional[str]:
    """The real path of carrier file `rel` under `root`, or None: not a
    .wav, not a regular file, or its real path is not inside root."""
    if not rel or "\0" in rel:
        return None
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(root.rstrip(os.sep) + os.sep):
        return None
    if not full.endswith(".wav") or not os.path.isfile(full):
        return None
    return full


# ---- WW0WWV vs WWV ------------------------------------------------------------

def day_epoch(date: str) -> int:
    return int(datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp())


def _finite(v):
    """JSON-safe: NaN and infinities become null, numpy scalars Python
    ones, recursively."""
    if hasattr(v, "item") and not isinstance(v, (list, tuple, dict, str)):
        try:
            v = v.item()
        except (TypeError, ValueError):
            pass
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_finite(x) for x in v]
    return v


class CarrierCache:
    """Per-file results, keyed by the file's real path, size and mtime;
    least recently used first out, with a separate bound per kind.  A
    summary is a few hundred bytes; a track (with the decode's JSON) is
    about 10 KB, so the defaults hold well under 10 MB."""

    DEFAULT_LIMITS = {"summary": 8000, "track": 300}

    def __init__(self, limits: Optional[Dict[str, int]] = None):
        self.limits = dict(self.DEFAULT_LIMITS, **(limits or {}))
        self._d: Dict[str, "OrderedDict"] = {}
        self._lock = threading.Lock()

    def size(self, what: str) -> int:
        with self._lock:
            return len(self._d.get(what, ()))

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    def get(self, full: str, what: str, compute):
        try:
            st = os.stat(full)
        except OSError:
            return None
        key = (full, st.st_size, st.st_mtime_ns)
        with self._lock:
            d = self._d.setdefault(what, OrderedDict())
            if key in d:
                d.move_to_end(key)
                return d[key]
        val = compute()
        with self._lock:
            d[key] = val
            d.move_to_end(key)
            while len(d) > self.limits.get(what, 100):
                d.popitem(last=False)
        return val


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


_BAD_FILES: set = set()


def _bad_file(path: str, exc: BaseException) -> None:
    """Log a carrier file that cannot be measured, once per path."""
    if path in _BAD_FILES:
        return
    if len(_BAD_FILES) > 10000:
        _BAD_FILES.clear()
    _BAD_FILES.add(path)
    logger.warning("carrier browser: skipping %s: %s: %s", path, type(exc).__name__, exc)


def ww0wwv_files(root: str, date: str, call: str = W.CALL) -> Iterator[dict]:
    """The WSPR-2 carriers of `call` (base call) of a date on the compared
    bands: receiver, band, hhmm, epoch, path (relative) and the file
    name's RF."""
    if not _DATE.fullmatch(date or ""):
        return
    day0 = day_epoch(date)
    want = base_call(call)
    for rx, band, fn in walk_date(root, date):
        if band not in W.WWV_FOR_BAND:
            continue
        m = NAME.match(fn)
        if not m or m.group(2) != "W2" or base_call(m.group(3)) != want:
            continue
        hh, mm = int(m.group(1)[:2]), int(m.group(1)[2:])
        if hh > 23 or mm > 59:
            continue
        yield {"receiver": rx, "band": band, "hhmm": m.group(1),
               "epoch": day0 + hh * 3600 + mm * 60,
               "path": f"{date}/{rx}/{band}/{fn}", "name_rf_hz": float(m.group(4))}


def _summary(root: str, f: dict, cache: CarrierCache) -> Optional[dict]:
    full = resolve_file(root, f["path"])
    if full is None:
        return None

    def compute():
        try:
            meta, rate, iq = W.read_carrier(full)
            s = W.carrier_summary(meta, rate, iq, f["band"], f["name_rf_hz"])
            if s is None:
                return None
            s["snr_wsprd"] = _num(meta.get("snr"))
            s["w50_hz"] = _num(meta.get("w50_hz"))
            s["message"] = str(meta.get("message") or "")[:80]
            return s
        except Exception as exc:                      # noqa: BLE001 -- a bad file
            _bad_file(f["path"], exc)
            return None
    return cache.get(full, "summary", compute)


def _num(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _alias_fields(ww0_ppb: Optional[float], wwv_ppb: Optional[float], mhz: float) -> dict:
    alias = W.wwv_may_alias(ww0_ppb, wwv_ppb, mhz)
    diff = None
    if not alias and ww0_ppb is not None and wwv_ppb is not None:
        diff = round(ww0_ppb - wwv_ppb, 4)
    return {"wwv_alias": alias, "diff_ppb": diff}


# ---- absolute levels: the recorder's calibrated noise rows -------------------------

def resolve_sink_path(*, config_file: str = SINK_CONFIG, legacy: str = LEGACY_SINK,
                      state_db: str = STATE_SINK) -> str:
    """The sink database the recorders write, found as hamsci_sink.sink_path
    finds it: $CARRIER_BROWSER_SINK_DB (or 'none'), $HAMSCI_SINK_PATH,
    $SIGMOND_SQLITE_PATH, `path` in /etc/hs-uploader/sink.toml,
    /var/lib/sigmond/sink.db if it exists, /var/lib/hs-uploader/sink.db if
    it exists, else the legacy path."""
    for v in (ENV_SINK_DB, "HAMSCI_SINK_PATH", "SIGMOND_SQLITE_PATH"):
        if (os.environ.get(v) or "").strip():
            return os.environ[v].strip()
    try:
        import tomllib
        with open(config_file, "rb") as fh:
            p = tomllib.load(fh).get("path")
        if isinstance(p, str) and p.strip():
            return p.strip()
    except (OSError, ValueError, ImportError):
        pass
    for cand in (legacy, state_db):
        if os.path.exists(cand):
            return cand
    return legacy


class NoiseSource:
    """Calibrated noise per receiver, band and WSPR cycle.  ``day(t0, t1)``
    returns ``{"available", "reason", "source", "rows": {(receiver dir,
    band, epoch): {"dbm_hz", "kind"}}}``.  This one has none."""

    name = "none"
    label = "no noise source configured"

    def day(self, t0: float, t1: float) -> dict:
        return {"available": False, "reason": self.label, "source": self.name, "rows": {}}


class SinkNoise(NoiseSource):
    """wspr-recorder's own noise rows, read from the sink (read-only).

    Each WSPR cycle the recorder writes one ``wspr.noise`` row per band
    (spot_sink.noise_to_row) into hamsci_sink's queue: table
    ``pending_uploads``, ``target_db`` 'wspr' (or $SIGMOND_SQLITE_DB_WSPR),
    ``target_table`` 'noise', the row as JSON in ``payload_json``: ``time``
    (cycle start, ISO Z), ``band``, ``reporter_id``, ``rx_source``,
    ``fft_noise_dbm`` and ``rms_noise_dbm``.  These are noise.py's
    calibrated dBm in 1 Hz (wsprdaemon's method, the KA9Q/RX888 per-band
    offsets measured against a calibrated KiwiSDR, plus a Kiwi source's
    noise_offset_db).  0.0 means not measured.  The FFT value (wsprd's
    own passband) is used, else the RMS one.  Rows are kept about 24 h
    (sigmond storage_trim, WSPR_RETENTION_MIN).
    """

    name = "wspr-recorder noise rows (sink.db wspr.noise)"

    def __init__(self, db_path: str, cache_s: float = 60.0):
        self.db_path = db_path
        self.cache_s = cache_s
        self._cache: Dict[tuple, Tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _alias(self) -> str:
        return os.environ.get("SIGMOND_SQLITE_DB_WSPR", "wspr")

    def query_sql(self, t0: float, t1: float) -> Tuple[str, tuple]:
        """Rows of cycles starting in [t0, t1), on hamsci_sink's
        idx_pending_uploads_cycle_time (the expression must match it)."""
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        lo = time.strftime(fmt, time.gmtime(max(0, t0)))
        hi = time.strftime(fmt, time.gmtime(max(0, t1)))
        q = ("SELECT payload_json FROM pending_uploads WHERE target_db = ? "
             "AND target_table = 'noise' "
             "AND json_extract(payload_json, '$.time') >= ? "
             "AND json_extract(payload_json, '$.time') < ?")
        return q, (self._alias(), lo, hi)

    def _none(self, reason: str) -> dict:
        return {"available": False, "reason": reason, "source": self.name, "rows": {}}

    def day(self, t0: float, t1: float) -> dict:
        key = (int(t0), int(t1), self._alias())
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.cache_s:
                return hit[1]
        out = self._read(t0, t1)
        with self._lock:
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[key] = (now, out)
        return out

    def _read(self, t0: float, t1: float) -> dict:
        if not os.path.isfile(self.db_path):
            return self._none(f"no sink database at {self.db_path}")
        q, args = self.query_sql(t0, t1)
        con = None
        try:
            con = sqlite3.connect("file:" + os.path.abspath(self.db_path) + "?mode=ro",
                                  uri=True, timeout=2)
            raw = con.execute(q, args).fetchall()
        except sqlite3.Error as exc:
            return self._none(f"cannot read noise rows from {self.db_path}: {exc}")
        finally:
            if con is not None:
                con.close()
        from .wav_writer import source_slug
        rows: dict = {}
        for (pj,) in raw:
            try:
                r = json.loads(pj)
                ts = int(datetime.strptime(r["time"], "%Y-%m-%dT%H:%M:%SZ")
                         .replace(tzinfo=timezone.utc).timestamp())
                band, rep = str(r["band"]), str(r.get("reporter_id") or r.get("radiod_id") or "")
            except (ValueError, KeyError, TypeError):
                continue
            val = None
            for kind in ("fft", "rms"):
                v = _num(r.get(f"{kind}_noise_dbm"))
                if v is not None and v != 0.0:
                    val = {"dbm_hz": v, "kind": kind}
                    break
            if val is None or not rep:
                continue
            names = [safe_name(rep)]
            src = str(r.get("rx_source") or "")
            if src:
                names.insert(0, safe_name(f"{rep}_{source_slug(src)}"))
            for n in names:
                rows.setdefault((n, band, ts), val)
        if not rows:
            return {"available": True, "reason": "no noise rows for this period (the sink "
                    "keeps them about 24 h)", "source": self.name, "rows": {}}
        return {"available": True, "reason": "", "source": self.name, "rows": rows}


WWV_LEVEL_NOTE = ("no calibrated WWV level: hf-timestd records no per-channel carrier power "
                  "(its L2_tick_timing has mean_snr_db, a relative tick SNR)")


def _levels(noise_day: dict) -> dict:
    return {"available": bool(noise_day.get("available")), "reason": noise_day.get("reason", ""),
            "source": noise_day.get("source", ""), "wwv": WWV_LEVEL_NOTE}


def _level_fields(noise_day: dict, receiver: str, band: str, epoch: int, snr) -> dict:
    n = noise_day.get("rows", {}).get((receiver, band, epoch))
    nd = n["dbm_hz"] if n else None
    return {"noise_dbm_hz": nd, "noise_kind": n["kind"] if n else None,
            "noise_dbm_2500": None if nd is None else round(W.noise_in_2500_dbm(nd), 2),
            "level_dbm": W.signal_dbm(snr, nd)}


def compare(root: str, date: str, wwv: W.WwvSource, cache: CarrierCache,
            call: str = W.CALL, noise: Optional[NoiseSource] = None) -> dict:
    """The main page's day: per band pair, WW0WWV's cycles and WWV's series."""
    out = {"date": date, "call": call, "wwv_source": wwv.name, "bands": [], "receivers": [],
           "levels": _levels(NoiseSource().day(0, 0))}
    if not _DATE.fullmatch(date or ""):
        return out
    day0 = day_epoch(date)
    nday = (noise or NoiseSource()).day(day0, day0 + 86400 + 120)
    out["levels"] = _levels(nday)
    by_band: dict = {b: [] for b, _ in W.BAND_PAIRS}
    rx_count: dict = {}
    for f in ww0wwv_files(root, date, call):
        s = _summary(root, f, cache)
        if s is None:
            continue
        rx_count[f["receiver"]] = rx_count.get(f["receiver"], 0) + 1
        by_band[f["band"]].append({
            "hhmm": f["hhmm"], "epoch": f["epoch"], "receiver": f["receiver"],
            "path": f["path"], "ppb": round(s["ppb"], 4), "hz": round(s["hz"], 6),
            "snr_wsprd": s.get("snr_wsprd"), "snr_carrier": round(s["snr_db"], 2),
            "w50_hz": s.get("w50_hz"), "message": s.get("message")})
    out["receivers"] = sorted(rx_count, key=lambda r: (-rx_count[r], r))
    for band, mhz in W.BAND_PAIRS:
        series = wwv.series(mhz, day0, day0 + 86400 + 120)
        cyc = sorted(by_band[band], key=lambda c: (c["epoch"], c["receiver"]))
        lim = W.wwv_limit_ppb(mhz)
        for p in series.get("points", []):
            p["edge"] = bool(abs(float(p["ppb"])) >= W.WWV_EDGE_FRACTION * lim)
        for c in cyc:
            c["wwv_ppb"] = W.wwv_over(series, c["epoch"], c["epoch"] + 120)
            c["wwv_hz"] = W.wwv_over(series, c["epoch"], c["epoch"] + 120, "hz")
            c.update(_alias_fields(c["ppb"], c["wwv_ppb"], mhz))
            c.update(_level_fields(nday, c["receiver"], band, c["epoch"], c["snr_wsprd"]))
        out["bands"].append({"band": band, "nominal_hz": W.NOMINAL_HZ[band],
                             "wwv_mhz": mhz, "wwv_limit_ppb": round(lim, 4),
                             "wwv_limit_hz": W.WWV_DOPPLER_LIMIT_HZ,
                             "cycles": cyc, "wwv": series})
    return out


def cycle_detail(root: str, date: str, hhmm: str, band: str, rx: str,
                 wwv: W.WwvSource, cache: CarrierCache, call: str = W.CALL,
                 noise: Optional[NoiseSource] = None) -> Optional[dict]:
    """One cycle of one band: WW0WWV's track and WWV's minutes around it."""
    if band not in W.WWV_FOR_BAND:
        return None
    found = [f for f in ww0wwv_files(root, date, call) if f["band"] == band and f["hhmm"] == hhmm]
    if not found:
        return None
    f = next((x for x in found if x["receiver"] == rx), found[0])
    full = resolve_file(root, f["path"])
    if full is None:
        return None

    def compute():
        try:
            meta, rate, iq = W.read_carrier(full)
            return {"meta": meta,
                    "summary": W.carrier_summary(meta, rate, iq, band, f["name_rf_hz"]),
                    "track": W.carrier_track(meta, rate, iq, band, f["epoch"], f["name_rf_hz"])}
        except Exception as exc:                      # noqa: BLE001 -- a bad file
            _bad_file(f["path"], exc)
            return {"error": f"cannot measure {f['path']}: {type(exc).__name__}"}
    d = cache.get(full, "track", compute)
    if d is None:
        return None
    if "error" in d:
        return d
    mhz = W.WWV_FOR_BAND[band]
    series = wwv.series(mhz, f["epoch"] - 120, f["epoch"] + 240)
    wwv_ppb = W.wwv_over(series, f["epoch"], f["epoch"] + 120)
    ww0_ppb = (d["summary"] or {}).get("ppb")
    nday = (noise or NoiseSource()).day(f["epoch"], f["epoch"] + 60)
    lv = _level_fields(nday, f["receiver"], band, f["epoch"], _num(d["meta"].get("snr")))
    track = d["track"]
    if track is not None:          # a copy: the cached track stays as measured
        nd = lv["noise_dbm_hz"]
        track = {**track, "points": [{**p, "level_dbm": W.signal_dbm(p.get("snr"), nd)}
                                     for p in track.get("points", [])]}
    return {**_alias_fields(ww0_ppb, wwv_ppb, mhz), **lv, "levels": _levels(nday),
            "wwv_hz": W.wwv_over(series, f["epoch"], f["epoch"] + 120, "hz"),
            "wwv_limit_hz": W.WWV_DOPPLER_LIMIT_HZ,
            "wwv_limit_ppb": round(W.wwv_limit_ppb(mhz), 4),
            "date": date, "hhmm": hhmm, "band": band, "epoch": f["epoch"],
            "receiver": f["receiver"], "receivers": sorted({x["receiver"] for x in found}),
            "path": f["path"], "rf_hz": f["name_rf_hz"], "nominal_hz": W.NOMINAL_HZ[band],
            "wwv_mhz": mhz, "meta": d["meta"], "summary": d["summary"], "track": track,
            "wwv": series, "wwv_ppb": wwv_ppb}


# ---- Host check ---------------------------------------------------------------

def _is_loopback(bind: str) -> bool:
    if bind == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def _host_name(header: str) -> str:
    h = (header or "").strip().lower()
    if h.startswith("["):                       # [::1]:8089
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def allowed_hosts(bind: str, extra: Iterable[str] = ()) -> set:
    """Names a request's Host header may carry (besides any IP literal):
    loopback names, this host's name, a named bind address, and `extra`."""
    names = set(LOOPBACK_HOSTS)
    try:
        hn = socket.gethostname().strip().lower()
        if hn:
            names.add(hn)
            names.add(hn.split(".")[0])
    except OSError:
        pass
    if bind and not _is_ip(bind):
        names.add(bind.lower())
    for e in extra:
        e = _host_name(e.strip())
        if e:
            names.add(e)
    return names


def host_ok(header: str, allowed: set) -> bool:
    """DNS rebinding guard.  A rebinding attack reaches us under the
    attacker's own host name, so only names are checked: an IP literal
    (the address the user typed) passes, a name must be in `allowed`."""
    name = _host_name(header)
    if not name:
        return False
    return _is_ip(name) or name.rstrip(".") in allowed


def _env_list(v: Optional[str]) -> List[str]:
    return [x.strip() for x in (v or "").replace(" ", ",").split(",") if x.strip()]


class Handler(BaseHTTPRequestHandler):
    server_version = "carrier-browser"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; img-src 'self' data:; "
                         "frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200, extra=None):
        return self.send(code, json.dumps(_finite(obj), allow_nan=False), extra=extra)

    def do_GET(self):
        srv = self.server
        if not host_ok(self.headers.get("Host", ""), srv.allowed_hosts):
            return self.json({"error": "host"}, 403)
        if not srv.slots.acquire(blocking=False):
            return self.json({"error": "busy"}, 503, {"Retry-After": "2"})
        try:
            return self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:                             # noqa: BLE001 -- never drop the client
            logger.warning("carrier browser: %s failed", self.path, exc_info=True)
            try:
                return self.json({"error": "internal error"}, 500)
            except OSError:
                pass
        finally:
            srv.slots.release()

    def _get(self):
        srv = self.server
        root = srv.root
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self.send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/compare":
                if not _DATE.fullmatch(q.get("date", "")):
                    return self.json({"error": "date"}, 400)
                date = q["date"]
                return self.json(srv.flight.do(
                    date, lambda: compare(root, date, srv.wwv, srv.cache, srv.call,
                                          noise=srv.noise)))
            if u.path == "/api/cycle":
                if not (_DATE.fullmatch(q.get("date", "")) and _HHMM.fullmatch(q.get("hhmm", ""))
                        and _BAND.fullmatch(q.get("band", ""))):
                    return self.json({"error": "date, hhmm or band"}, 400)
                d = cycle_detail(root, q["date"], q["hhmm"], q["band"], q.get("rx", ""),
                                 srv.wwv, srv.cache, srv.call, noise=srv.noise)
                if d is None:
                    return self.json({"error": "no such cycle"}, 404)
                if "error" in d:
                    return self.json(d, 422)
                return self.json(d)
            if u.path == "/api/dates":
                return self.json(dates(root))
            if u.path == "/api/cycles":
                return self.json(cycles(root, q.get("date", "")))
            if u.path == "/api/spots":
                if not _HHMM.fullmatch(q.get("hhmm", "")):
                    return self.json({"error": "hhmm"}, 400)
                return self.json(spots(root, q.get("date", ""), q["hhmm"]))
            if u.path == "/file":
                full = resolve_file(root, q.get("path", ""))
                if full is None or os.path.getsize(full) > MAX_FILE_BYTES:
                    return self.json({"error": "no such file"}, 404)
                with open(full, "rb") as f:
                    data = f.read(MAX_FILE_BYTES + 1)
                name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(full))
                return self.send(200, data, "audio/wav",
                                 {"Content-Disposition": f'attachment; filename="{name}"'})
            return self.json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except OSError:
            return self.json({"error": "no such file"}, 404)


def build_server(root, bind: str = DEFAULT_BIND, port: int = DEFAULT_PORT, *,
                 wwv: Optional[W.WwvSource] = None,
                 extra_hosts: Optional[Iterable[str]] = None,
                 call: str = W.CALL,
                 noise: Optional[NoiseSource] = None) -> ThreadingHTTPServer:
    """The server for archive `root` (not created if missing: read-only).
    Refuses / as the root: that would serve every .wav on the host.
    `wwv`: where WWV's Doppler comes from (default: none).  `noise`: the
    calibrated noise rows behind the dBm levels (default: none).  `extra_hosts`:
    more Host names to accept (default: $CARRIER_BROWSER_ALLOWED_HOSTS)."""
    if os.path.realpath(str(root)) == os.sep:
        raise ValueError("refusing to serve / as the carrier archive root")
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    cls = type("CarrierBrowserServer", (ThreadingHTTPServer,), {"address_family": family})
    srv = cls((bind, port), Handler)
    srv.daemon_threads = True
    srv.root = os.path.realpath(str(root))
    srv.loopback_only = _is_loopback(bind)
    if extra_hosts is None:
        extra_hosts = _env_list(os.environ.get(ENV_ALLOWED))
    srv.allowed_hosts = allowed_hosts(bind, extra_hosts)
    srv.wwv = wwv or W.WwvSource()
    srv.noise = noise or NoiseSource()
    srv.cache = CarrierCache()
    srv.flight = SingleFlight()
    srv.slots = threading.BoundedSemaphore(MAX_CONCURRENT)
    srv.call = call
    return srv


def parse_args(argv=None) -> argparse.Namespace:
    env = os.environ
    ap = argparse.ArgumentParser(prog="python -m wspr_recorder.carrier_browser",
                                 description="Browse wspr-recorder's carrier archive (read-only).")
    ap.add_argument("--root", required=True, help="the carrier archive ([carrier_archive] root)")
    ap.add_argument("--bind", default=env.get(ENV_BIND) or DEFAULT_BIND,
                    help=f"address to listen on (default ${ENV_BIND}, else 127.0.0.1; "
                         f"0.0.0.0 or a LAN address serves the network: there is no "
                         f"authentication)")
    ap.add_argument("--port", type=int, default=int(env.get(ENV_PORT) or DEFAULT_PORT),
                    help=f"default ${ENV_PORT}, else {DEFAULT_PORT}")
    ap.add_argument("--timestd-db", default=env.get(ENV_TIMESTD_DB) or DEFAULT_TIMESTD_DB,
                    help=f"hf-timestd's SQLite database, for WWV's Doppler (default "
                         f"${ENV_TIMESTD_DB}, else {DEFAULT_TIMESTD_DB}; 'none' for no WWV)")
    ap.add_argument("--sink-db", default=None,
                    help=f"the sink database with wspr-recorder's noise rows, for signal "
                         f"levels in dBm (default ${ENV_SINK_DB}, else found as hamsci_sink "
                         f"finds it; 'none' for no levels)")
    ap.add_argument("--allow-host", action="append", default=None, metavar="NAME",
                    help=f"also accept this Host name (repeatable; default "
                         f"${ENV_ALLOWED}, comma-separated)")
    ap.add_argument("--call", default=W.CALL, help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    wwv = W.WwvSource() if a.timestd_db.lower() == "none" else W.TimestdWwv(a.timestd_db)
    sink = a.sink_db or resolve_sink_path()
    noise = NoiseSource() if sink.lower() == "none" else SinkNoise(sink)
    extra = a.allow_host if a.allow_host is not None else _env_list(os.environ.get(ENV_ALLOWED))
    try:
        srv = build_server(a.root, a.bind, a.port, wwv=wwv, extra_hosts=extra, call=a.call,
                           noise=noise)
    except ValueError as exc:
        print(f"carrier-browser: {exc}", file=sys.stderr, flush=True)
        sys.exit(2)
    if not srv.loopback_only:
        print(f"carrier-browser: WARNING: listening on {a.bind}, not loopback: "
              f"anyone who can reach it can read the archive (no authentication)",
              file=sys.stderr, flush=True)
    print(f"carrier-browser: {srv.root} at http://{a.bind}:{a.port}/; WWV from "
          f"{getattr(wwv, 'db_path', 'nowhere')}; noise rows from "
          f"{getattr(noise, 'db_path', 'nowhere')}; Host names accepted: "
          f"{', '.join(sorted(srv.allowed_hosts))} and any IP address", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>WW0WWV vs WWV</title>
<style>
:root{--bg:#fbfbfa;--fg:#1d1f21;--muted:#6b6f75;--line:#dcdfe3;--card:#fff;--acc:#1f6fb5;--a2:#d9730d;--sel:#e8f1fa;--zero:#9aa0a6}
@media (prefers-color-scheme: dark){:root{--bg:#16181b;--fg:#e6e6e6;--muted:#9aa0a6;--line:#2c3036;--card:#1e2125;--acc:#6fb3f2;--a2:#f2a35e;--sel:#203040;--zero:#5b6067}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
h1{font-size:16px;margin:0 8px 0 0}h2{font-size:15px;margin:0}label{color:var(--muted);font-size:12px;margin-right:4px}
select,button{font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
button{cursor:pointer}button[aria-pressed=true]{background:var(--sel);border-color:var(--acc)}
.views{display:flex;gap:4px}
#spotsView main{display:grid;grid-template-columns:minmax(320px,1fr) minmax(360px,1.4fr);gap:16px;padding:16px}
@media (max-width:900px){#spotsView main{grid-template-columns:1fr}}
#compareView{padding:16px;display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
.bar{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;padding:12px 16px 0}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600}tr.spot{cursor:pointer}tr.spot:hover{background:var(--sel)}tr.on{background:var(--sel)}
tr.band td{font-weight:600;padding-top:10px;color:var(--muted)}.num{text-align:right}.muted{color:var(--muted)}
canvas{width:100%;display:block;margin-top:8px}canvas.click{cursor:crosshair}.legend span{margin-right:12px;font-size:12px}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
#info{font-size:13px}#info b,.dinfo b{font-weight:600}a{color:var(--acc)}
.tablewrap{overflow-x:auto;max-height:75vh;overflow-y:auto}
.sub{font-size:12px;color:var(--muted);margin-top:2px;font-variant-numeric:tabular-nums}
.drill{border-top:1px solid var(--line);margin-top:12px;padding-top:8px}.dinfo{font-size:13px;font-variant-numeric:tabular-nums}
.drillgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media (max-width:900px){.drillgrid{grid-template-columns:1fr}}
.warn{color:var(--a2)}[hidden]{display:none!important}
</style></head><body>
<header><h1>WW0WWV vs WWV</h1>
<span class="views" role="group" aria-label="View"><button id="vCompare" aria-pressed="true">Comparison</button><button id="vSpots" aria-pressed="false">All spots</button></span>
<span><label for="date">Date (UTC)</label><button id="dprev" title="Day before">&#9664;</button> <select id="date"></select> <button id="dnext" title="Day after">&#9654;</button></span>
<span id="rxwrap"><label for="crx">Receiver</label><select id="crx"></select></span>
<span id="unitwrap"><label>Doppler in</label><span class="views" role="group" aria-label="Doppler unit"><button id="uPpb" aria-pressed="true" title="parts per billion of each nominal: the comparison">ppb</button><button id="uHz" aria-pressed="false" title="each signal's offset in Hz from its own nominal">Hz</button></span></span>
<span id="status" class="muted"></span>
</header>
<section id="compareView" aria-label="WW0WWV against WWV">
<p class="muted" style="margin:0">WW0WWV's carrier (the WWV-site WSPR beacon, its modulation removed) against WWV's own carrier on the nearest frequency.
Doppler is in parts per billion of each nominal frequency, so the two compare directly: on a shared path they should move together.
<span style="color:var(--a2)">&#9632;</span> WW0WWV, one point per WSPR cycle (its whole transmission);
<span style="color:var(--acc)">&#9473;</span> WWV, one value per minute from hf-timestd. Click a WW0WWV point for that cycle.
<b>ppb</b> is the comparison; <b>Hz</b> shows each signal's own offset from its nominal (WWV's is hf-timestd's doppler_hz), as other charts do, but Hz at different carrier frequencies do not compare directly.
Signal levels are dBm at the antenna feed: wsprd's SNR plus the station's calibrated noise floor (wspr-recorder's noise rows, the same calibration as its wsprdaemon noise graphs).
hf-timestd resolves WWV's Doppler only within &plusmn;0.5 Hz (dashed lines); a hollow square marks a cycle where WWV may be aliased, and no difference is given for it.</p>
<div id="pairs"></div>
</section>
<section id="spotsView" hidden>
<div class="bar">
<span><label for="cycle">Cycle</label><button id="prev" title="Earlier cycle">&#9664;</button> <select id="cycle"></select> <button id="next" title="Later cycle">&#9654;</button></span>
<span><label for="rx">Receiver</label><select id="rx"><option value="">all</option></select></span>
<span><label for="band">Band</label><select id="band"><option value="">all</option></select></span>
<span><label><input type="checkbox" id="overlay"> overlay spots (Doppler in ppb)</label></span>
</div>
<main>
<section class="card"><div class="tablewrap"><table><thead><tr><th>Call</th><th>Message</th><th class="num">SNR</th><th class="num">Frequency, Hz</th><th class="num">Spread, Hz</th><th>Mode</th></tr></thead><tbody id="spots"></tbody></table></div><p id="count" class="muted"></p></section>
<section class="card"><div id="info" class="muted">Pick a spot to plot its carrier.</div>
<canvas id="spec" height="220" aria-label="Spectrogram"></canvas>
<canvas id="dop" height="220" aria-label="Doppler"></canvas><div class="legend" id="legend"></div>
<canvas id="snr" height="140" aria-label="SNR"></canvas></section>
</main>
</section>
<script>
"use strict";
const $=id=>document.getElementById(id);
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);}
function num(v,d){const x=Number(v);return v==null||v===""||!isFinite(x)?"":x.toFixed(d);}
function sgn(v,d){const s=num(v,d);return s===""?"?":(Number(v)>0?"+":"")+s;}
function css(n){return getComputedStyle(document.body).getPropertyValue(n).trim();}
const COLORS=["#1f6fb5","#d9730d","#2a9d55","#b5341f","#7a4fb5","#8a6d1f","#1f8f9a"];
let spotsData=[], overlay=[], view="compare", cmp=null, sel={}, unit="ppb";
function uKey(){return unit==="hz"?"hz":"ppb";}
function uLabel(){return unit==="hz"?"Offset, Hz from nominal (each signal its own)":"Doppler, ppb from nominal";}
async function j(url){const r=await fetch(url);if(!r.ok)throw new Error(url+": "+r.status);return r.json();}
function opt(sel,v,t){const o=document.createElement("option");o.value=v;o.textContent=t??v;sel.appendChild(o);}
function hhmm(h){return h.slice(0,2)+":"+h.slice(2);}
function pad2(n){return String(n).padStart(2,"0");}
function utc(t){const d=new Date(t*1000);return pad2(d.getUTCHours())+":"+pad2(d.getUTCMinutes());}
function pst(t){return utc(t-8*3600);}
function todayUTC(){const d=new Date();return d.getUTCFullYear()+pad2(d.getUTCMonth()+1)+pad2(d.getUTCDate());}
function dayLabel(x){return x.slice(0,4)+"-"+x.slice(4,6)+"-"+x.slice(6);}
function hashParams(){return new URLSearchParams(location.hash.slice(1));}
function setHash(p){const q=new URLSearchParams();for(const [k,v] of Object.entries(p))if(v)q.set(k,v);history.replaceState(null,"","#"+q.toString());}

// ---- canvas plumbing ----
function frame(cv,xr,yr,xticks,yl,opts){opts=opts||{};const dpr=window.devicePixelRatio||1,W=cv.clientWidth||600;if(!cv.dataset.h)cv.dataset.h=cv.getAttribute("height");const H=+cv.dataset.h;
 cv.width=W*dpr;cv.height=H*dpr;cv.style.height=H+"px";
 const g=cv.getContext("2d");g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,W,H);const fg=css("--fg"),mu=css("--muted"),ln=css("--line");
 const L=58,R=10,T=18,B=opts.two?40:30,w=W-L-R,h=H-T-B;g.font="11px system-ui";g.strokeStyle=ln;g.fillStyle=mu;g.lineWidth=1;
 const X=x=>L+(x-xr[0])/(xr[1]-xr[0])*w,Y=y=>T+h-(y-yr[0])/(yr[1]-yr[0])*h;
 for(let i=0;i<=5;i++){const y=yr[0]+(yr[1]-yr[0])*i/5;g.beginPath();g.moveTo(L,Y(y));g.lineTo(L+w,Y(y));g.stroke();g.fillText(+y.toPrecision(3)+"",4,Y(y)+4);}
 for(const t of xticks){const x=X(t[0]);g.beginPath();g.moveTo(x,T);g.lineTo(x,T+h);g.stroke();g.fillText(t[1],x-g.measureText(t[1]).width/2,T+h+13);if(t[2])g.fillText(t[2],x-g.measureText(t[2]).width/2,T+h+26);}
 if(opts.two){g.fillText("UTC",2,T+h+13);g.fillText("PST",2,T+h+26);}else if(opts.xl)g.fillText(opts.xl,L+w/2-g.measureText(opts.xl).width/2,T+h+27);
 if(yr[0]<0&&yr[1]>0){g.strokeStyle=css("--zero");g.beginPath();g.moveTo(L,Y(0));g.lineTo(L+w,Y(0));g.stroke();}
 g.fillStyle=fg;g.fillText(yl,L,12);
 cv._map={xr,L,w};return {g,X,Y,L,T,w,h};}
function clip(A){A.g.save();A.g.beginPath();A.g.rect(A.L,A.T,A.w,A.h);A.g.clip();}
function series(A,s){const g=A.g;g.strokeStyle=g.fillStyle=s.color;g.lineWidth=s.lw||1.5;
 if(s.kind==="line"){g.beginPath();let prev=null;for(const p of s.pts){if(p[1]==null){prev=null;continue;}const x=A.X(p[0]),y=A.Y(p[1]);(prev!==null&&(!s.gap||p[0]-prev<=s.gap))?g.lineTo(x,y):g.moveTo(x,y);prev=p[0];}g.stroke();}
 else if(s.kind==="step"){for(const p of s.pts){if(p[2]==null)continue;g.beginPath();g.moveTo(A.X(p[0]),A.Y(p[2]));g.lineTo(A.X(p[1]),A.Y(p[2]));g.stroke();}}
 else if(s.kind==="hline"){g.setLineDash([4,3]);g.beginPath();g.moveTo(A.L,A.Y(s.y));g.lineTo(A.L+A.w,A.Y(s.y));g.stroke();g.setLineDash([]);}
 else{const r=s.r||3;for(const p of s.pts){if(p[1]==null)continue;const x=A.X(p[0]),y=A.Y(p[1]);if(s.kind==="sq")g.fillRect(x-r,y-r,2*r,2*r);else if(s.kind==="hollow")g.strokeRect(x-r,y-r,2*r,2*r);else{g.beginPath();g.arc(x,y,r,0,7);g.fill();}}}}
function pct(a,q){if(!a.length)return null;const s=[...a].sort((x,y)=>x-y),i=(s.length-1)*q,lo=Math.floor(i),hi=Math.ceil(i);return s[lo]+(s[hi]-s[lo])*(i-lo);}
function yrange(vals,minPad,robust){vals=vals.filter(v=>v!=null&&isFinite(v));if(!vals.length)return [-minPad,minPad];
 let lo=robust?pct(vals,0.02):Math.min(...vals),hi=robust?pct(vals,0.98):Math.max(...vals);const p=Math.max(minPad,0.3*(hi-lo));return [lo-p,hi+p];}

// ---- comparison view ----
function rxSel(){return $("crx").value;}
async function loadCompare(){const date=$("date").value;$("status").textContent="Loading "+dayLabel(date)+" ...";
 try{cmp=await j("/api/compare?date="+date);}catch(e){$("status").textContent=String(e);return;}
 const cur=rxSel();$("crx").innerHTML="";cmp.receivers.forEach(r=>opt($("crx"),r));if(cmp.receivers.includes(cur))$("crx").value=cur;
 $("rxwrap").hidden=view!=="compare"||cmp.receivers.length<2;
 const n=cmp.bands.reduce((a,b)=>a+b.cycles.length,0);$("status").textContent=n?"":"No "+cmp.call+" carriers on "+dayLabel(date)+".";
 drawCompare();}
function bandCycles(b){const rx=rxSel();return b.cycles.filter(c=>!rx||c.receiver===rx);}
function drawCompare(){if(!cmp)return;const box=$("pairs");box.innerHTML="";const day0=Date.UTC(+cmp.date.slice(0,4),+cmp.date.slice(4,6)-1,+cmp.date.slice(6))/1000;
 const xr=[day0,day0+86400],xt=[];for(let h=0;h<=24;h+=3)xt.push([day0+h*3600,pad2(h%24)+":00",pad2((h+16)%24)+":00"]);
 const cw=css("--a2"),cv=css("--acc");
 for(const b of cmp.bands){const cyc=bandCycles(b),wv=b.wwv||{points:[]};const card=document.createElement("div");card.className="card";card.id="pair"+b.band;
  const diffs=cyc.filter(c=>c.diff_ppb!=null).map(c=>c.diff_ppb),nal=cyc.filter(c=>c.wwv_alias).length,lim=unit==="hz"?b.wwv_limit_hz:b.wwv_limit_ppb,K=uKey(),lv=cmp.levels||{};
  const wtxt=wv.available?"WWV "+esc(b.wwv_mhz)+" MHz: "+esc(wv.points.length)+" minutes from "+esc(wv.source)+" ("+esc(wv.channel)+")":'<span class="warn">no WWV data</span>: '+esc(wv.reason);
  card.innerHTML="<h2>"+esc(b.band)+" m WW0WWV &middot; WWV "+esc(b.wwv_mhz)+" MHz</h2><div class=\"sub\">"+esc(cmp.call)+" nominal "+esc(num(b.nominal_hz,3))+" Hz &middot; "+esc(cyc.length)+" cycles &middot; "+wtxt
   +" &middot; WWV resolves &plusmn;"+esc(num(lim,1))+" ppb (&plusmn;"+esc(b.wwv_limit_hz)+" Hz)"
   +(diffs.length?" &middot; median WW0WWV &minus; WWV "+esc(sgn(pct(diffs,0.5),2))+" ppb over "+esc(diffs.length)+" cycles":"")
   +(nal?' &middot; <span class="warn">WWV may be aliased in '+esc(nal)+" cycle"+(nal===1?"":"s")+"</span> (hollow squares; no difference given)":"")+"</div>"
   +'<canvas class="click dopc" height="230" aria-label="Doppler over the day"></canvas><canvas class="snrc" height="120" aria-label="SNR over the day"></canvas><canvas class="lvlc" height="130" aria-label="Signal level over the day"></canvas>'
   +'<div class="sub">Level: '+(lv.available?"calibration from "+esc(lv.source)+(lv.reason?" &middot; "+esc(lv.reason):""):'<span class="warn">no WW0WWV level</span>: '+esc(lv.reason))+" &middot; WWV: "+esc(lv.wwv||"")+"</div>"
   +'<div class="legend"><span><i class="sw" style="background:'+esc(cw)+'"></i>WW0WWV '+esc(b.band)+' m, whole transmission</span><span><i class="sw" style="background:'+esc(cv)+'"></i>WWV '+esc(b.wwv_mhz)+' MHz, per minute</span></div><div class="drill" hidden></div>';
  box.appendChild(card);
  const dop=card.querySelector(".dopc"),sn=card.querySelector(".snrc");
  card._redraw=()=>{
  const wp=wv.points.map(p=>[p.t+30,p[K]]),sp=cyc.filter(c=>!c.wwv_alias).map(c=>[c.epoch+60,c[K]]),ap=cyc.filter(c=>c.wwv_alias).map(c=>[c.epoch+60,c[K]]);
  let A=frame(dop,xr,yrange(wp.map(p=>p[1]).concat(sp.map(p=>p[1]),ap.map(p=>p[1])),unit==="hz"?0.05:5,true),xt,uLabel()+" (dashed: WWV's alias limit)",{two:true});
  clip(A);if(lim){series(A,{kind:"hline",y:lim,color:css("--muted"),lw:1});series(A,{kind:"hline",y:-lim,color:css("--muted"),lw:1});}
  series(A,{kind:"line",pts:wp,color:cv,gap:180,lw:1.2});series(A,{kind:"dot",pts:wp,color:cv,r:1.2});series(A,{kind:"sq",pts:sp,color:cw,r:3});series(A,{kind:"hollow",pts:ap,color:cw,r:3});
  if(sel[b.band])series(A,{kind:"line",pts:[[sel[b.band],-1e9],[sel[b.band],1e9]],color:css("--muted"),lw:1});A.g.restore();
  const ws=wv.points.filter(p=>p.snr!=null).map(p=>[p.t+30,p.snr]),ss=cyc.filter(c=>c.snr_wsprd!=null).map(c=>[c.epoch+60,Number(c.snr_wsprd)]);
  A=frame(sn,xr,yrange(ws.map(p=>p[1]).concat(ss.map(p=>p[1])),3,false),xt.map(t=>[t[0],"",""]),"SNR, dB: WW0WWV wsprd (2500 Hz); WWV hf-timestd tick SNR",{});
  clip(A);series(A,{kind:"line",pts:ws,color:cv,gap:180,lw:1});series(A,{kind:"sq",pts:ss,color:cw,r:2.5});A.g.restore();
  const ls=cyc.filter(c=>c.level_dbm!=null).map(c=>[c.epoch+60,c.level_dbm]),ns=cyc.filter(c=>c.noise_dbm_2500!=null).map(c=>[c.epoch+60,c.noise_dbm_2500]);
  A=frame(card.querySelector(".lvlc"),xr,yrange(ls.map(p=>p[1]).concat(ns.map(p=>p[1])),3,false),xt.map(t=>[t[0],"",""]),"Signal, dBm: WW0WWV (squares); noise floor in 2500 Hz (grey)",{});
  clip(A);series(A,{kind:"line",pts:ns,color:css("--muted"),gap:600,lw:1});series(A,{kind:"sq",pts:ls,color:cw,r:2.5});A.g.restore();};
  card._redraw();
  dop.onclick=e=>{const m=dop._map,r=dop.getBoundingClientRect(),px=e.clientX-r.left;let best=null,bd=12;
   for(const c of cyc){const x=m.L+(c.epoch+60-m.xr[0])/(m.xr[1]-m.xr[0])*m.w,d=Math.abs(x-px);if(d<bd){bd=d;best=c;}}
   if(best)drill(b,best,card);};}
 const p=hashParams();if(p.get("band")&&p.get("hhmm")){const b=cmp.bands.find(x=>x.band===p.get("band"));const c=b&&bandCycles(b).find(x=>x.hhmm===p.get("hhmm"));if(c)drill(b,c,$("pair"+b.band));}}
async function drill(b,c,card){const el=card.querySelector(".drill");el.hidden=false;el.innerHTML='<div class="dinfo muted">Loading '+esc(hhmm(c.hhmm))+" ...</div>";
 setHash({date:cmp.date,band:b.band,hhmm:c.hhmm});
 let d;try{d=await j("/api/cycle?date="+cmp.date+"&hhmm="+c.hhmm+"&band="+encodeURIComponent(b.band)+"&rx="+encodeURIComponent(c.receiver));}catch(e){el.innerHTML='<div class="dinfo">'+esc(e)+"</div>";return;}
 const s=d.summary||{},m=d.meta||{},wv=d.wwv||{points:[]},cw=css("--a2"),cv=css("--acc"),list=bandCycles(b),i=list.indexOf(c);
 const diff=d.diff_ppb;
 el.innerHTML='<div class="dinfo"><button class="dp" title="Earlier cycle">&#9664;</button> <button class="dn" title="Later cycle">&#9654;</button> <b>'+esc(b.band)+" m, "+esc(hhmm(d.hhmm))+" UTC</b> ("+esc(pst(d.epoch))+" PST) &middot; "+esc(m.message)+" &middot; "+esc(d.receiver)
  +" &middot; SNR "+esc(sgn(m.snr,0))+" dB (wsprd)<br>WW0WWV over the transmission <b>"+esc(sgn(s.ppb,2))+" ppb</b> ("+esc(sgn(s.hz*1000,1))+" mHz) &middot; WWV "+esc(d.wwv_mhz)+" MHz over the same minutes <b>"
  +(d.wwv_ppb!=null?esc(sgn(d.wwv_ppb,2))+" ppb</b> ("+esc(wv.channel)+")":'</b><span class="warn">no WWV data</span> ('+esc(wv.reason)+")")
  +(d.wwv_alias?' &middot; <b class="warn">WWV may be aliased</b> (WWV '+esc(d.wwv_mhz)+" MHz resolves only &plusmn;"+esc(num(d.wwv_limit_ppb,1))+" ppb; no difference given)":"")
  +(diff!=null?" &middot; difference <b>"+esc(sgn(diff,2))+" ppb</b>":"")
  +(d.wwv_hz!=null?" &middot; WWV offset "+esc(sgn(d.wwv_hz*1000,1))+" mHz":"")
  +"<br>WW0WWV level "+(d.level_dbm!=null?"<b>"+esc(num(d.level_dbm,1))+" dBm</b> (wsprd SNR "+esc(sgn(m.snr,0))+" dB + noise "+esc(num(d.noise_dbm_hz,1))+" dBm/Hz, "+esc(d.noise_kind)+", over 2500 Hz)":'<span class="warn">unknown</span> ('+esc((d.levels||{}).reason||"no noise row for this cycle")+")")
  +' &middot; <a href="/file?path='+encodeURIComponent(d.path)+'">download .wav</a> &middot; <a href="#view=spots&date='+esc(d.date)+"&hhmm="+esc(d.hhmm)+'" class="tospots">all spots of this cycle</a></div>'
  +'<div class="drillgrid"><div><canvas class="dspec" height="200" aria-label="WW0WWV spectrogram"></canvas></div><div><canvas class="dtrack" height="200" aria-label="Doppler track"></canvas><canvas class="dsnr" height="110" aria-label="SNR track"></canvas><canvas class="dlvl" height="110" aria-label="Level track"></canvas>'
  +'<div class="legend"><span><i class="sw" style="background:'+esc(cw)+'"></i>WW0WWV, '+esc(d.track?d.track.win_s:"")+' s windows every 2 s</span><span><i class="sw" style="background:'+esc(cv)+'"></i>WWV, each minute</span></div></div></div>';
 el.querySelector(".dp").onclick=()=>{if(i>0)drill(b,list[i-1],card);};el.querySelector(".dn").onclick=()=>{if(i>=0&&i<list.length-1)drill(b,list[i+1],card);};
 el.querySelector(".tospots").onclick=e=>{e.preventDefault();showView("spots",d.hhmm);};
 sel[b.band]=c.epoch+60;card._redraw&&card._redraw();
 const xr=[0,120],xt=[0,20,40,60,80,100,120].map(x=>[x,x+" s"]),E=d.epoch;
 const K=uKey(),tp=(d.track?d.track.points:[]).map(p=>[p.t-E,p[K]]),wm=wv.points.filter(p=>p.t>=E-60&&p.t<E+180).map(p=>[p.t-E,p.t-E+60,p[K]]);
 let A=frame(el.querySelector(".dtrack"),xr,yrange(tp.map(p=>p[1]).concat(wm.map(p=>p[2])),unit==="hz"?0.03:3,false),xt,uLabel(),{xl:"seconds into the cycle"});
 clip(A);series(A,{kind:"step",pts:wm,color:cv,lw:3});series(A,{kind:"line",pts:tp,color:cw});series(A,{kind:"dot",pts:tp,color:cw,r:2});if(s[K]!=null)series(A,{kind:"hline",y:s[K],color:cw,lw:1});A.g.restore();
 const ts=(d.track?d.track.points:[]).filter(p=>p.snr!=null).map(p=>[p.t-E,p.snr]),wsn=wv.points.filter(p=>p.t>=E-60&&p.t<E+180).map(p=>[p.t-E,p.t-E+60,p.snr]);
 A=frame(el.querySelector(".dsnr"),xr,yrange(ts.map(p=>p[1]).concat(wsn.map(p=>p[2])),3,false),xt,"SNR, dB",{});
 clip(A);series(A,{kind:"step",pts:wsn,color:cv,lw:3});series(A,{kind:"line",pts:ts,color:cw});A.g.restore();
 const tl=(d.track?d.track.points:[]).filter(p=>p.level_dbm!=null).map(p=>[p.t-E,p.level_dbm]),n25=d.noise_dbm_2500;
 A=frame(el.querySelector(".dlvl"),xr,yrange(tl.map(p=>p[1]).concat(n25!=null?[n25]:[]),3,false),xt,tl.length?"Signal, dBm (window SNR + noise); grey: noise in 2500 Hz":"Signal, dBm: no noise row for this cycle",{});
 clip(A);if(n25!=null)series(A,{kind:"hline",y:n25,color:css("--muted"),lw:1});series(A,{kind:"line",pts:tl,color:cw});A.g.restore();
 try{const buf=await (await fetch("/file?path="+encodeURIComponent(d.path))).arrayBuffer();const cr=parseWav(buf);const rf=(m.dial_mhz!=null&&m.ref_hz!=null)?m.dial_mhz*1e6+m.ref_hz:d.rf_hz;
  drawSpec(cr,el.querySelector(".dspec"),{off:rf-d.nominal_hz,yr:[-1.5,1.5],xr:[0,120],label:"WW0WWV, Hz from nominal "+num(d.nominal_hz,3)});}catch(e){}
}

// ---- all-spots view (and the carrier maths it plots in the browser) ----
async function loadCycles(want){const c=await j("/api/cycles?date="+$("date").value);$("cycle").innerHTML="";c.forEach(x=>opt($("cycle"),x.hhmm,hhmm(x.hhmm)+"  ("+x.spots+")"));
 if(!c.length){$("spots").innerHTML="";$("count").textContent="No carrier files on this date.";return;}
 if(want&&c.some(x=>x.hhmm===want))$("cycle").value=want;await loadSpots();}
async function loadSpots(){if(!$("cycle").value){$("spots").innerHTML="";return;}
 setHash({view:"spots",date:$("date").value,hhmm:$("cycle").value});
 spotsData=await j("/api/spots?date="+$("date").value+"&hhmm="+$("cycle").value);
 for(const [id,key] of [["rx","receiver"],["band","band"]]){const cur=$(id).value;$(id).innerHTML='<option value="">all</option>';[...new Set(spotsData.map(s=>s[key]))].sort((a,b)=>isNaN(a)?a.localeCompare(b):b-a).forEach(v=>opt($(id),v));if([...$(id).options].some(o=>o.value===cur))$(id).value=cur;}
 render();}
function render(){const tb=$("spots");tb.innerHTML="";const rx=$("rx").value,band=$("band").value;
 const list=spotsData.filter(s=>(!rx||s.receiver===rx)&&(!band||s.band===band));let lastKey="";
 for(const s of list){const key=s.receiver+" · "+s.band+" m";if(key!==lastKey){const tr=document.createElement("tr");tr.className="band";tr.innerHTML='<td colspan="6">'+esc(key)+"</td>";tb.appendChild(tr);lastKey=key;}
  const m=s.meta||{};const tr=document.createElement("tr");tr.className="spot";tr.tabIndex=0;
  const fine=s.rf_hz-(Number(m.ref_hz)||0)+(m.fine_hz!=null?Number(m.fine_hz):(Number(m.ref_hz)||0));  // RF: the file name's (dial + ref) moved to the fine frequency
  tr.innerHTML="<td>"+esc(s.call)+'</td><td class="muted">'+esc(m.message)+'</td><td class="num">'+esc(m.snr)+'</td><td class="num">'+num(fine,3)+'</td><td class="num">'+num(m.w50_hz,3)+"</td><td>"+esc(s.mode)+"</td>";
  tr.onclick=()=>pick(s,tr);tr.onkeydown=e=>{if(e.key==="Enter")pick(s,tr);};tb.appendChild(tr);}
 $("count").textContent=list.length+" spot"+(list.length===1?"":"s")+" in this cycle";}
function parseWav(buf){const v=new DataView(buf);let i=12,rate=15,meta={},iq=null;
 while(i+8<=v.byteLength){const id=String.fromCharCode(...new Uint8Array(buf,i,4)),n=v.getUint32(i+4,true);
  if(id==="fmt ")rate=v.getUint32(i+12,true);
  else if(id==="LIST"){const t=new TextDecoder().decode(new Uint8Array(buf,i+20,v.getUint32(i+16,true)));try{meta=JSON.parse(t.split("\0")[0]);}catch(e){}}
  else if(id==="data"){iq=new Float32Array(buf.slice(i+8,i+8+(n&~7)));break;}
  i+=8+n+(n&1);}
 return {rate,meta,iq};}
function fft(re,im){const n=re.length;for(let i=1,j=0;i<n;i++){let b=n>>1;for(;j&b;b>>=1)j^=b;j^=b;if(i<j){[re[i],re[j]]=[re[j],re[i]];[im[i],im[j]]=[im[j],im[i]];}}
 for(let len=2;len<=n;len<<=1){const a=-2*Math.PI/len,wr=Math.cos(a),wi=Math.sin(a);for(let i=0;i<n;i+=len){let cr=1,ci=0;for(let k=0;k<len/2;k++){const p=i+k,q=p+len/2,tr=re[q]*cr-im[q]*ci,ti=re[q]*ci+im[q]*cr;re[q]=re[p]-tr;im[q]=im[p]-ti;re[p]+=tr;im[p]+=ti;const t=cr*wr-ci*wi;ci=cr*wi+ci*wr;cr=t;}}}}
function spectrum(iq,s,n,nfft,win){const re=new Float64Array(nfft),im=new Float64Array(nfft);for(let k=0;k<n;k++){const w=win?0.5-0.5*Math.cos(2*Math.PI*k/(n-1)):1;re[k]=iq[2*(s+k)]*w;im[k]=iq[2*(s+k)+1]*w;}
 fft(re,im);const p=new Float64Array(nfft);for(let k=0;k<nfft;k++)p[k]=re[k]*re[k]+im[k]*im[k];return p;}
function fq(k,nfft,rate){return (k>=nfft/2?k-nfft:k)*rate/nfft;}
function peak(iq,s,n,rate,centre,span){const nfft=1<<Math.ceil(Math.log2(n*64)),p=spectrum(iq,s,n,nfft,false);let best=-1,bk=0;
 for(let k=0;k<nfft;k++){const f=fq(k,nfft,rate);if(centre!=null&&Math.abs(f-centre)>span)continue;if(p[k]>best){best=p[k];bk=k;}}
 const a=p[(bk-1+nfft)%nfft],b=p[bk],c=p[(bk+1)%nfft],den=a-2*b+c,d=den?0.5*(a-c)/den:0;return fq(bk,nfft,rate)+d*rate/nfft;}
function snr(iq,s,n,rate){const nfft=1<<Math.ceil(Math.log2(n)),p=spectrum(iq,s,n,nfft,true);let bk=0;for(let k=1;k<nfft;k++)if(p[k]>p[bk])bk=k;
 const fk=fq(bk,nfft,rate);let sig=0,ns=0,noise=0,nn=0;for(let k=0;k<nfft;k++){const d=Math.abs(fq(k,nfft,rate)-fk);if(d<0.5){sig+=p[k];ns++;}else if(d>3){noise+=p[k];nn++;}}
 noise/=Math.max(nn,1);return 10*Math.log10(Math.max(sig-noise*ns,1e-30)/(noise/(rate/n)*2500));}
function track(c){const {iq,rate,meta}=c,N=iq.length/2,win=(meta.snr!=null&&meta.snr<=-15)?30:10,nw=Math.min(N,Math.round(win*rate)),ns=Math.round(2*rate);
 const whole=peak(iq,0,N,rate),pts=[];for(let s=0;s+nw<=N;s+=ns)pts.push({t:(meta.t0_s||0)+(s+nw/2)/rate,f:peak(iq,s,nw,rate,whole,2),snr:snr(iq,s,nw,rate)});
 return {whole,win,pts};}
function axes(cv,xr,yr,xl,yl){const xt=[];for(let i=0;i<=6;i++){const x=xr[0]+(xr[1]-xr[0])*i/6;xt.push([x,Math.round(x)+""]);}return frame(cv,xr,yr,xt,yl,{xl});}
function drawSpec(c,cv,o){o=o||{};const {iq,rate,meta}=c,N=iq.length/2,n=Math.round(10*rate),hop=Math.round(rate),nfft=1024,span=3,off=o.off||0;const cols=[];
 if(N<n)return;
 for(let s=0;s+n<=N;s+=hop){const p=spectrum(iq,s,n,nfft,true),col=[];for(let k=0;k<nfft;k++){const f=fq(k,nfft,rate);if(Math.abs(f)<=span)col.push([f,10*Math.log10(p[k]+1e-30)]);}col.sort((a,b)=>a[0]-b[0]);cols.push(col);}
 const t0=(meta.t0_s||0)+5,t1=t0+(cols.length-1),xr=o.xr||[t0-5,t1+5],yr=o.yr||[-span,span];const A=axes(cv,xr,yr,"seconds into the cycle",o.label||("Hz from "+(c.ref).toFixed(3)+" Hz (decoder's frequency)"));
 let mx=-1e9;cols.forEach(col=>col.forEach(v=>{if(v[1]>mx)mx=v[1];}));const img=A.g;const cw=Math.max(1,A.w*(hop/rate)/(xr[1]-xr[0])),rh=A.h*(rate/nfft)/(yr[1]-yr[0]);
 clip(A);cols.forEach((col,i)=>col.forEach(v=>{const z=Math.max(0,Math.min(1,(v[1]-(mx-40))/40));img.fillStyle="hsl("+(240-240*z)+",80%,"+(15+45*z)+"%)";img.fillRect(A.X(t0+i)-cw/2,A.Y(v[0]+off)-rh/2,cw+0.5,rh+0.5);}));img.restore();}
function plotLines(cv,series,yl,key){let xs=[],ys=[];series.forEach(s=>s.pts.forEach(p=>{xs.push(p.t);ys.push(p[key]);}));if(!xs.length)return;
 let y0=Math.min(...ys),y1=Math.max(...ys);if(y1-y0<1e-6){y0-=1;y1+=1;}const pad=(y1-y0)*0.1;const A=axes(cv,[Math.min(...xs)-5,Math.max(...xs)+5],[y0-pad,y1+pad],"seconds into the cycle",yl);
 series.forEach(s=>{A.g.strokeStyle=s.color;A.g.fillStyle=s.color;A.g.lineWidth=1.5;A.g.beginPath();s.pts.forEach((p,i)=>{const x=A.X(p.t),y=A.Y(p[key]);i?A.g.lineTo(x,y):A.g.moveTo(x,y);});A.g.stroke();s.pts.forEach(p=>A.g.fillRect(A.X(p.t)-1.5,A.Y(p[key])-1.5,3,3));});}
async function pick(s,tr){document.querySelectorAll("tr.on").forEach(r=>{if(!$("overlay").checked)r.classList.remove("on");});tr.classList.add("on");
 $("info").textContent="Loading "+s.call+" ...";const buf=await (await fetch("/file?path="+encodeURIComponent(s.path))).arrayBuffer();const c=parseWav(buf);
 c.ref=s.rf_hz;const t=track(c);
 const label=s.call+" "+s.band+" m "+s.receiver;const mean=s.rf_hz+t.whole;  // the file name's RF is the carrier's reference
 const entry={label,color:COLORS[overlay.length%COLORS.length],pts:t.pts.map(p=>({t:p.t,hz:(p.f-t.whole)*1000,ppb:(p.f-t.whole)/mean*1e9,snr:p.snr}))};
 overlay=$("overlay").checked?overlay.concat([entry]):[entry];
 const m=c.meta||{};$("info").innerHTML="<b>"+esc(s.call)+"</b> "+esc(m.message)+" &middot; "+esc(s.receiver)+" "+esc(s.band)+" m &middot; "+esc(s.mode)+" &middot; SNR "+(esc(m.snr)||"?")+" dB &middot; spread "+(num(m.w50_hz,3)||"?")+" Hz<br>frequency over the transmission "+num(mean,4)+" Hz &middot; "+esc(t.win)+" s windows &middot; <a href=\"/file?path="+encodeURIComponent(s.path)+"\">download .wav</a>";
 drawSpec(c,$("spec"));
 if($("overlay").checked)plotLines($("dop"),overlay,"Doppler, ppb from each spot's mean","ppb");else plotLines($("dop"),overlay,"mHz from the transmission's mean","hz");
 plotLines($("snr"),overlay,"SNR, dB in 2500 Hz","snr");
 $("legend").innerHTML=overlay.map(o=>'<span><i class="sw" style="background:'+esc(o.color)+'"></i>'+esc(o.label)+"</span>").join("");}

// ---- views, dates ----
function showView(v,want){view=v;$("compareView").hidden=v!=="compare";$("spotsView").hidden=v!=="spots";
 $("vCompare").setAttribute("aria-pressed",v==="compare");$("vSpots").setAttribute("aria-pressed",v==="spots");
 $("rxwrap").hidden=v!=="compare"||!cmp||cmp.receivers.length<2;$("unitwrap").hidden=v!=="compare";$("status").textContent="";
 if(v==="compare"){setHash({date:$("date").value});loadCompare();}else{setHash({view:"spots",date:$("date").value,hhmm:want});loadCycles(want).catch(e=>{$("count").textContent=String(e);});}}
function onDate(){showView(view);}
async function init(){const p=hashParams();let d=[];try{d=await j("/api/dates");}catch(e){$("status").textContent=String(e);}
 const today=todayUTC();if(!d.includes(today))d.unshift(today);d.sort().reverse();$("date").innerHTML="";d.forEach(x=>opt($("date"),x,dayLabel(x)));
 $("date").value=(p.get("date")&&d.includes(p.get("date")))?p.get("date"):today;
 showView(p.get("view")==="spots"?"spots":"compare",p.get("hhmm"));}
function setUnit(u){unit=u;$("uPpb").setAttribute("aria-pressed",u==="ppb");$("uHz").setAttribute("aria-pressed",u==="hz");try{localStorage.setItem("cbUnit",u);}catch(e){}drawCompare();}
try{const u=localStorage.getItem("cbUnit");if(u==="hz"||u==="ppb"){unit=u;$("uPpb").setAttribute("aria-pressed",u==="ppb");$("uHz").setAttribute("aria-pressed",u==="hz");}}catch(e){}
$("uPpb").onclick=()=>setUnit("ppb");$("uHz").onclick=()=>setUnit("hz");
$("vCompare").onclick=()=>showView("compare");$("vSpots").onclick=()=>showView("spots");
$("date").onchange=onDate;$("crx").onchange=drawCompare;
$("dprev").onclick=()=>{const s=$("date");if(s.selectedIndex<s.options.length-1){s.selectedIndex++;onDate();}};
$("dnext").onclick=()=>{const s=$("date");if(s.selectedIndex>0){s.selectedIndex--;onDate();}};
$("cycle").onchange=loadSpots;$("rx").onchange=render;$("band").onchange=render;
$("prev").onclick=()=>{const s=$("cycle");if(s.selectedIndex<s.options.length-1){s.selectedIndex++;loadSpots();}};
$("next").onclick=()=>{const s=$("cycle");if(s.selectedIndex>0){s.selectedIndex--;loadSpots();}};
$("overlay").onchange=()=>{overlay=[];document.querySelectorAll("tr.on").forEach(r=>r.classList.remove("on"));};
let rs=null;window.addEventListener("resize",()=>{clearTimeout(rs);rs=setTimeout(()=>{if(view==="compare")drawCompare();},200);});
init();
</script></body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
