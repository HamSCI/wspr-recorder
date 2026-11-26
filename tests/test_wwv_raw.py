"""WWV's carrier measured on hf-timestd's raw IQ buffers (wwv_raw.py).

The synthetic files are what hf-timestd writes: complex64 at 24 kHz with
the carrier at 0 Hz, zstd-compressed (by the zstd CLI, as on a station),
beside a .json sidecar.  They are a minute or two long, not ten, and
their samples are rounded to 1/8 so they compress to a few MB.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from wspr_recorder import carrier_browser as cb
from wspr_recorder import ww0wwv as W
from wspr_recorder import wwv_raw as R

from tests.test_ww0wwv_compare import (EPOCH_2056, _json, _serve, archive,  # noqa: F401
                                       make_timestd)

REPO = Path(__file__).resolve().parent.parent
FS = 24000
T0 = 1795629600                        # 2026-11-25 14:40 UTC, a 10-minute boundary

needs_zstd = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd CLI not installed")


def make_raw(root: Path, channel: str, epoch: int, seconds: int, carriers, noise=0.3,
             freq_hz=None, completeness=100.0, dtype="complex64") -> Path:
    """One raw-buffer file: <root>/<channel>/<yyyymmdd>/<epoch>.bin.zst + .json."""
    d = root / channel / time.strftime("%Y%m%d", time.gmtime(epoch))
    d.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(epoch % 1000 + len(channel))
    raw = d / f"{epoch}.bin"
    with open(raw, "wb") as f:
        for s in range(0, seconds * FS, 10 * FS):
            n = min(10 * FS, seconds * FS - s)
            t = (s + np.arange(n)) / FS
            x = noise * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
            for hz, amp in carriers:
                x = x + amp * np.exp(2j * np.pi * hz * t)
            x = np.round(x * 8) / 8
            if dtype == "complex-f16":   # hf-timestd's default: interleaved binary16 x 4096
                f.write((x.astype("<c8").view("<f4") * 4096.0).astype("<f2").tobytes())
            else:
                f.write(x.astype("<c8").tobytes())
    subprocess.run(["zstd", "-q", "-f", "--rm", str(raw), "-o", str(raw) + ".zst"], check=True)
    if freq_hz is None:
        freq_hz = int(channel.split("_")[1]) * 1000
    (d / f"{epoch}.json").write_text(json.dumps({
        "minute_boundary": epoch, "channel_name": channel, "frequency_hz": freq_hz,
        "sample_rate": FS, "samples_written": seconds * FS, "samples_expected": 600 * FS,
        "file_duration_sec": 600, "completeness_pct": completeness, "dtype": dtype,
        "byte_order": "little", "compression": "zstd"}))
    return d / f"{epoch}.bin.zst"


def src(tmp_path, roots=None, **kw):
    kw.setdefault("background", False)
    return R.RawBufferWwv(roots or [str(tmp_path / "raw")], str(tmp_path / "cache"), **kw)


# ---- the measurement ----------------------------------------------------------------

@needs_zstd
def test_known_offset_within_5_mhz(tmp_path):
    make_raw(tmp_path / "raw", "WWV_20000", T0, 120, [(-0.155, 1.0)])
    s = src(tmp_path).series(20.0, T0, T0 + 600)
    assert s["available"] and s["channel"] == "WWV_20000" and s["blend"] == "WWV"
    assert s["source"] == "raw IQ (hf-timestd raw_buffer)" and s["reliable"] is True
    assert s["limit_hz"] == 1.5
    assert [p["t"] for p in s["points"]] == [T0, T0 + 60]
    for p in s["points"]:
        assert abs(p["hz"] - (-0.155)) < 0.005
        assert p["ppb"] == pytest.approx(p["hz"] / 20e6 * 1e9, abs=1e-3)
        assert p["snr"] > 40 and p["q"] == "ok" and p["ok"] and p["two"] is False


@needs_zstd
def test_two_carriers_on_a_shared_channel(tmp_path):
    # WWV +0.10 Hz and WWVH -0.30 Hz, 6 dB weaker
    make_raw(tmp_path / "raw", "SHARED_10000", T0, 60, [(0.10, 1.0), (-0.30, 0.5)])
    s = src(tmp_path).series(10.0, T0, T0 + 600)
    assert s["blend"] == "WWV+WWVH" and s["channel"] == "SHARED_10000"
    (p,) = s["points"]
    assert p["two"] is True
    assert abs(p["hz"] - 0.10) < 0.005 and abs(p["hz2"] - (-0.30)) < 0.005
    assert p["snr"] - p["snr2"] == pytest.approx(6.0, abs=1.0)
    assert p["ppb2"] == pytest.approx(-30.0, abs=0.5)


def test_carrier_at_dc_is_nominal_and_offset_applied():
    assert R.nominal_offset_hz({"frequency_hz": 20000000}, 20.0) == 0.0
    assert R.nominal_offset_hz({}, 20.0) == 0.0
    assert R.nominal_offset_hz({"frequency_hz": 10000001}, 10.0) == pytest.approx(1.0)


def _tone(hz, amp=1.0, noise=0.01, n=6000, seed=0):
    t = np.arange(n) / 100.0
    rng = np.random.default_rng(seed)
    return amp * np.exp(2j * np.pi * hz * t) + noise * (rng.standard_normal(n)
                                                        + 1j * rng.standard_normal(n))


def test_quality_flags_and_resolution():
    (ok,) = R.measure_decimated(_tone(0.2), 100.0, T0)
    assert ok["q"] == "ok" and abs(ok["hz"] - 0.2) < 0.002 and ok["hz2"] is None
    (edge,) = R.measure_decimated(_tone(1.45), 100.0, T0)
    assert edge["q"] == "edge"                       # beyond 90 % of +-1.5 Hz
    (weak,) = R.measure_decimated(_tone(0.2, amp=0.0, noise=1.0), 100.0, T0)
    assert weak["q"] == "weak"
    (gaps,) = R.measure_decimated(_tone(0.2), 100.0, T0, gaps=True)
    assert gaps["q"] == "gaps"
    # 0.02 Hz apart: not resolved in 60 s (Hann main lobe), one carrier
    (one,) = R.measure_decimated(_tone(0.2) + 0.8 * _tone(0.22, seed=1), 100.0, T0)
    assert one["hz2"] is None
    # a second peak more than TWO_CARRIER_DB down is not reported
    (faint,) = R.measure_decimated(_tone(0.2) + 0.1 * _tone(-0.4, seed=1), 100.0, T0)
    assert faint["hz2"] is None


@needs_zstd
def test_noise_only_is_weak_and_unavailable(tmp_path):
    make_raw(tmp_path / "raw", "WWV_25000", T0, 60, [])
    s = src(tmp_path).series(25.0, T0, T0 + 600)
    assert not s["available"] and [p["q"] for p in s["points"]] == ["weak"]
    assert W.wwv_over(s, T0, T0 + 120) is None        # weak minutes are not used


# ---- chunked decimation: memory bounded by structure ----------------------------------

class _Reader(io.RawIOBase):
    def __init__(self, data: bytes):
        self.b = io.BytesIO(data)
        self.largest = 0

    def readinto(self, mv):
        self.largest = max(self.largest, len(mv))
        return self.b.readinto(mv)


def test_decimate_stream_reads_bounded_chunks():
    rng = np.random.default_rng(3)
    x = (rng.standard_normal(240 * 1000) + 1j * rng.standard_normal(240 * 1000)).astype("<c8")
    r = _Reader(x.tobytes() + b"\0" * 8 * 17)        # and a partial block at the end
    y = R.decimate_stream(r, 240, 10 ** 9, chunk_samples=240 * 64)
    assert r.largest <= 240 * 64 * 8                   # never more than a chunk at once
    np.testing.assert_allclose(y, x.reshape(-1, 240).mean(axis=1), rtol=1e-6, atol=1e-6)
    # max_samples caps what is read (a runaway stream)
    r2 = _Reader(x.tobytes())
    assert len(R.decimate_stream(r2, 240, 240 * 100, chunk_samples=240 * 64)) == 100
    assert R.CHUNK_SAMPLES * 8 < 16 * 2 ** 20          # the default chunk is ~7.7 MB


def test_measure_file_streams_through_zstd_not_a_whole_read():
    import inspect
    code = inspect.getsource(R.measure_file)
    assert "decimate_stream(proc.stdout" in code and "zstd" in code
    assert ".read()" not in code and "capture_output" not in code


# ---- the cache, lazily and once ----------------------------------------------------------

@needs_zstd
def test_cache_hit_miss_and_invalidation(tmp_path, monkeypatch):
    f = make_raw(tmp_path / "raw", "WWV_20000", T0, 60, [(0.05, 1.0)])
    calls = []
    real = R.measure_file
    monkeypatch.setattr(R, "measure_file", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    w = src(tmp_path)
    a = w.series(20.0, T0, T0 + 600)
    b = w.series(20.0, T0, T0 + 600)
    assert len(calls) == 1 and a["points"] == b["points"]
    # survives a restart: the cache is on disk
    assert (tmp_path / "cache" / "wwv-carrier.sqlite").is_file()
    c = src(tmp_path).series(20.0, T0, T0 + 600)
    assert len(calls) == 1 and c["points"] == a["points"]
    # a rewritten file is measured again
    st = os.stat(f)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
    src(tmp_path).series(20.0, T0, T0 + 600)
    assert len(calls) == 2
    # and once the raw buffer is gone, the cache still answers
    shutil.rmtree(tmp_path / "raw")
    d = src(tmp_path).series(20.0, T0, T0 + 600)
    assert d["available"] and d["channel"] == "WWV_20000" and len(calls) == 2


@needs_zstd
def test_single_flight_one_measurement_per_file(tmp_path, monkeypatch):
    make_raw(tmp_path / "raw", "WWV_20000", T0, 60, [(0.05, 1.0)])
    started, release, calls = threading.Event(), threading.Event(), []
    real = R.measure_file

    def slow(*a, **k):
        calls.append(1)
        started.set()
        release.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(R, "measure_file", slow)
    w = src(tmp_path)
    out = []
    th = [threading.Thread(target=lambda: out.append(w.series(20.0, T0, T0 + 600)))
          for _ in range(3)]
    th[0].start()
    assert started.wait(10)
    for t in th[1:]:
        t.start()
    time.sleep(0.3)
    release.set()
    for t in th:
        t.join(20)
    assert len(calls) == 1 and len(out) == 3 and all(o["available"] for o in out)


@needs_zstd
def test_many_files_are_queued_not_measured_in_the_request(tmp_path, monkeypatch):
    make_raw(tmp_path / "raw", "WWV_20000", T0, 60, [(0.05, 1.0)])
    make_raw(tmp_path / "raw", "WWV_20000", T0 + 600, 60, [(0.06, 1.0)])
    w = src(tmp_path, sync_files=1)
    s = w.series(20.0, T0, T0 + 1200)
    assert w.measured == 0 and s["pending"] == 2 and not s["available"]
    assert "queued" in s["reason"]
    assert w.drain() == 2
    s = w.series(20.0, T0, T0 + 1200)
    assert s["available"] and s["pending"] == 0 and [p["t"] for p in s["points"]] == [T0, T0 + 600]


@needs_zstd
def test_bad_file_recorded_once_not_retried(tmp_path, monkeypatch):
    d = tmp_path / "raw" / "WWV_20000" / time.strftime("%Y%m%d", time.gmtime(T0))
    d.mkdir(parents=True)
    (d / f"{T0}.bin.zst").write_bytes(b"not zstd at all")
    (d / f"{T0}.json").write_text(json.dumps({"minute_boundary": T0, "sample_rate": FS}))
    w = src(tmp_path)
    s = w.series(20.0, T0, T0 + 600)
    assert not s["available"] and w.measured == 1
    w.series(20.0, T0, T0 + 600)
    assert w.measured == 1


def test_unwritable_cache_falls_back_to_memory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    st = R.CarrierStore(str(blocker / "cache"))
    assert st.path is None
    st.put("WWV_20000", T0, 1, 2, 100.0, [{"t": T0, "hz": 0.1, "snr": 50.0, "q": "ok",
                                         "hz2": None, "snr2": None}], None, 0.1)
    assert st.known("WWV_20000", [T0]) == {T0: (1, 2)}
    assert len(st.minutes("WWV_20000", T0, T0 + 60)) == 1
    st.prune(T0 + 1)
    assert st.minutes("WWV_20000", T0, T0 + 60) == []


# ---- the fallback ------------------------------------------------------------------------

def test_fallback_only_without_raw_buffer_and_labelled_unreliable(tmp_path):
    db = make_timestd(tmp_path / "timestd.db", [
        (EPOCH_2056, "SHARED_10000", "WWV", 10.0, 12.0, -0.1)])
    w = src(tmp_path, fallback=W.TimestdWwv(str(db)))
    s = w.series(10.0, EPOCH_2056 - 60, EPOCH_2056 + 120)
    assert s["source"] == "hf-timestd L2 doppler_hz (unreliable)"
    assert s["reliable"] is False and s["limit_hz"] == 0.5 and s["available"]
    # without a fallback: no WWV, and why
    s = src(tmp_path).series(10.0, EPOCH_2056 - 60, EPOCH_2056 + 120)
    assert not s["available"] and "no raw buffer" in s["reason"]


@needs_zstd
def test_raw_buffer_wins_over_the_fallback(tmp_path):
    db = make_timestd(tmp_path / "timestd.db", [(T0, "WWV_20000", "WWV", 20.0, 12.0, 0.4)])
    make_raw(tmp_path / "raw", "WWV_20000", T0, 60, [(0.05, 1.0)])
    s = src(tmp_path, fallback=W.TimestdWwv(str(db))).series(20.0, T0, T0 + 600)
    assert s["source"].startswith("raw IQ") and abs(s["points"][0]["hz"] - 0.05) < 0.005


# ---- end to end -----------------------------------------------------------------------------

@needs_zstd
def test_api_compare_and_cycle_with_raw_wwv(archive, tmp_path):  # noqa: F811
    # WWV 10 MHz at -0.13 Hz (-13 ppb) over the 20:56 cycle; 20 MHz at +0.05 Hz
    make_raw(tmp_path / "raw", "SHARED_10000", EPOCH_2056, 120, [(-0.13, 1.0)])
    make_raw(tmp_path / "raw", "WWV_20000", EPOCH_2056, 60, [(0.05, 1.0)])
    srv = _serve(archive, wwv=src(tmp_path))
    try:
        d = _json(srv, "/api/compare?date=20261115")
        c = _json(srv, "/api/cycle?date=20261115&hhmm=2056&band=30")
    finally:
        srv.shutdown(); srv.server_close()
    by = {b["band"]: b for b in d["bands"]}
    b30 = by["30"]
    assert b30["wwv"]["source"].startswith("raw IQ") and b30["wwv"]["channel"] == "SHARED_10000"
    assert b30["wwv_blend"] == "WWV+WWVH" and by["15"]["wwv_blend"] == "WWV"
    assert b30["wwv_limit_hz"] == 1.5 and b30["wwv_limit_ppb"] == pytest.approx(150.0)
    assert [p["t"] for p in b30["wwv"]["points"]] == [EPOCH_2056, EPOCH_2056 + 60]
    cy = b30["cycles"][0]
    assert cy["wwv_ppb"] == pytest.approx(-13.0, abs=0.5)
    assert cy["wwv_hz"] == pytest.approx(-0.13, abs=0.005)
    assert cy["wwv_alias"] is False and cy["wwv_unreliable"] is False and cy["wwv_two"] is False
    assert cy["diff_ppb"] == pytest.approx(cy["ppb"] - cy["wwv_ppb"], abs=1e-3)
    assert by["15"]["wwv"]["available"] and by["15"]["wwv"]["points"][0]["hz"] == pytest.approx(0.05, abs=0.005)
    assert not by["12"]["wwv"]["available"]           # no WWV_25000 buffer
    # the drill-down: WWV's two one-minute points over the cycle
    assert c["wwv_blend"] == "WWV+WWVH" and c["wwv_limit_hz"] == 1.5
    assert [p["t"] for p in c["wwv"]["points"]] == [EPOCH_2056, EPOCH_2056 + 60]
    assert c["wwv_ppb"] == pytest.approx(-13.0, abs=0.5) and c["diff_ppb"] is not None


# ---- wiring ---------------------------------------------------------------------------------

def test_cli_builds_the_raw_source_with_fallback(tmp_path, monkeypatch):
    for k in (cb.ENV_RAW_BUFFER, cb.ENV_WWV_CACHE, cb.ENV_WWV_PREFILL, cb.ENV_TIMESTD_DB):
        monkeypatch.delenv(k, raising=False)
    a = cb.parse_args(["--root", "/x", "--wwv-cache-dir", str(tmp_path / "c"), "--wwv-prefill", "0"])
    assert a.raw_buffer == "/var/lib/timestd/raw_buffer:/dev/shm/timestd/raw_buffer"
    w = cb.make_wwv(a)
    assert isinstance(w, R.RawBufferWwv) and isinstance(w.fallback, W.TimestdWwv)
    assert w.roots == ["/var/lib/timestd/raw_buffer", "/dev/shm/timestd/raw_buffer"]
    assert w.store.path == str(tmp_path / "c" / "wwv-carrier.sqlite") and not w.prefill
    a = cb.parse_args(["--root", "/x", "--raw-buffer", "none"])
    assert isinstance(cb.make_wwv(a), W.TimestdWwv)
    a = cb.parse_args(["--root", "/x", "--raw-buffer", "none", "--timestd-db", "none"])
    assert type(cb.make_wwv(a)) is W.WwvSource
    assert R.cache_dir_from(None, {"CACHE_DIRECTORY": "/var/cache/a:/var/cache/b"}) == "/var/cache/a"
    assert R.cache_dir_from(None, {}) == "/var/cache/wspr-carrier-browser"


def test_unit_gives_a_cache_directory_and_raw_roots():
    lines = [ln.strip() for ln in
             (REPO / "systemd" / "wspr-carrier-browser.service").read_text().splitlines()]
    assert "CacheDirectory=wspr-carrier-browser" in lines
    assert ("Environment=TIMESTD_RAW_BUFFER=/var/lib/timestd/raw_buffer:"
            "/dev/shm/timestd/raw_buffer") in lines
    assert "MemoryMax=256M" in lines and "CPUQuota=50%" in lines


def test_page_and_docs_explain_the_source_change():
    assert "WWV+WWVH" in cb.PAGE and "two carriers" in cb.PAGE and "1.5 Hz" in cb.PAGE
    doc = (REPO / "docs" / "CARRIER-ARCHIVE.md").read_text()
    assert "raw_buffer" in doc and "WWV+WWVH" in doc and "noise" in doc
    assert "5.0.0" in doc and "unreliable" in doc


# ---- fix round 1 ----------------------------------------------------------------------------

@needs_zstd
def test_at_most_two_measurements_at_once(tmp_path, monkeypatch):
    f = make_raw(tmp_path / "raw", "WWV_20000", T0, 60, [(0.05, 1.0)])
    meta = json.loads((f.parent / f"{T0}.json").read_text())
    lock, now, peak = threading.Lock(), [0], [0]
    real = R.decimate_stream

    def slow(*a, **k):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.2)
        try:
            return real(*a, **k)
        finally:
            with lock:
                now[0] -= 1
    monkeypatch.setattr(R, "decimate_stream", slow)
    th = [threading.Thread(target=R.measure_file, args=(str(f), meta, 20.0)) for _ in range(5)]
    for t in th:
        t.start()
    for t in th:
        t.join(30)
    assert peak[0] == 2


@needs_zstd
def test_zstd_runs_with_a_memory_limit(tmp_path, monkeypatch):
    f = make_raw(tmp_path / "raw", "WWV_20000", T0, 60, [(0.05, 1.0)])
    meta = json.loads((f.parent / f"{T0}.json").read_text())
    seen = []
    real = subprocess.Popen
    monkeypatch.setattr(R.subprocess, "Popen", lambda args, **k: seen.append(args) or real(args, **k))
    assert R.measure_file(str(f), meta, 20.0)["rows"]
    assert "--memory=64MB" in seen[0]


def test_non_finite_is_weak_and_never_written():
    nan = float("nan")
    assert R.quality({"hz": nan, "snr": 50.0}, False) == "weak"
    assert R.quality({"hz": 0.1, "snr": nan}, False) == "weak"
    assert R.quality({"hz": 0.1, "snr": float("inf")}, False) == "weak"
    y = _tone(0.2)
    y[100] = nan
    rows = R.measure_decimated(np.concatenate([y, _tone(0.3)]), 100.0, T0)
    for r in rows:
        assert r["hz"] is not None and np.isfinite(r["hz"])
        assert r["snr"] is None or np.isfinite(r["snr"])
    assert [r["t"] for r in rows if r["q"] != "weak"] == [T0 + 60]


@needs_zstd
def test_store_write_failure_still_returns_the_measurement(tmp_path, monkeypatch, caplog):
    make_raw(tmp_path / "raw", "WWV_20000", T0, 60, [(0.05, 1.0)])
    w = src(tmp_path)

    def boom(*a, **k):
        raise R.sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(w.store, "put", boom)
    s = w.series(20.0, T0, T0 + 600)
    assert s["available"] and abs(s["points"][0]["hz"] - 0.05) < 0.005
    w.series(20.0, T0, T0 + 600)
    assert sum("disk I/O error" in r.getMessage() for r in caplog.records) == 1


def test_cache_read_errors_degrade(tmp_path, monkeypatch):
    (tmp_path / "raw" / "WWV_20000").mkdir(parents=True)
    w = src(tmp_path)

    def boom(*a, **k):
        raise R.sqlite3.DatabaseError("file is not a database")
    for name in ("known", "minutes", "channels_with_rows"):
        monkeypatch.setattr(w.store, name, boom)
    s = w.series(20.0, T0, T0 + 600)
    assert s["available"] is False and "cache" in s["reason"]


def test_memory_store_is_one_shared_connection(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    st = R.CarrierStore(str(blocker / "cache"))
    assert st.path is None and not hasattr(st, "_uri")
    errs = []

    def work(k):
        try:
            for i in range(20):
                st.put(f"C{k}", T0 + 600 * i, 1, 2, 100.0,
                       [{"t": T0 + 600 * i, "hz": 0.1, "snr": 50.0, "q": "ok",
                         "hz2": None, "snr2": None}], None, 0.1)
                st.minutes(f"C{k}", T0, T0 + 10 ** 6)
                st.known(f"C{k}", [T0])
        except Exception as exc:                     # noqa: BLE001
            errs.append(exc)
    th = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in th:
        t.start()
    for t in th:
        t.join(30)
    assert errs == [] and len(st.minutes("C0", T0, T0 + 10 ** 6)) == 20


@pytest.mark.parametrize("rc, err, killed, kind", [
    (1, "zstd: x.zst: unsupported format", False, "corrupt"),
    (1, "x.zst : Read error (39) : premature end", False, "corrupt"),
    (1, "x.zst : Decoding error (36) : Data corruption detected", False, "corrupt"),
    (1, "x.zst : Decoding error (36) : Frame requires too much memory for decoding", False,
     "transient"),
    (1, "x.zst: Permission denied", False, "transient"),
    (-9, "", False, "transient"),
    (0, "", True, "transient"),
])
def test_zstd_failures_classified(rc, err, killed, kind):
    assert R.classify_zstd(rc, err, killed) == kind


@needs_zstd
def test_transient_errors_expire_corrupt_ones_stay(tmp_path, monkeypatch):
    make_raw(tmp_path / "raw", "WWV_20000", T0, 60, [(0.05, 1.0)])
    calls, real = [], R.measure_file

    def flaky(*a, **k):
        calls.append(1)
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(R, "measure_file", flaky)
    w = src(tmp_path)
    w.series(20.0, T0, T0 + 600)
    w.series(20.0, T0, T0 + 600)
    assert len(calls) == 1                           # not hammered
    t = time.time()
    monkeypatch.setattr(R, "_now", lambda: t + 601)
    w.series(20.0, T0, T0 + 600)
    assert len(calls) == 2                           # but retried after 10 minutes
    # a corrupt file stays cached
    d = tmp_path / "raw" / "WWV_25000" / time.strftime("%Y%m%d", time.gmtime(T0))
    d.mkdir(parents=True)
    (d / f"{T0}.bin.zst").write_bytes(b"not zstd at all")
    (d / f"{T0}.json").write_text(json.dumps({"minute_boundary": T0, "sample_rate": FS}))
    monkeypatch.setattr(R, "measure_file", real)
    w.series(25.0, T0, T0 + 600)
    n = w.measured
    monkeypatch.setattr(R, "_now", lambda: t + 10 ** 6)
    w.series(25.0, T0, T0 + 600)
    assert w.measured == n


def test_watchdog_kills_a_stuck_zstd(tmp_path, monkeypatch):
    fake = tmp_path / "zstd"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setattr(R, "READ_TIMEOUT_S", 0.5)
    t = time.monotonic()
    with pytest.raises(R.TransientError):
        R.measure_file(str(tmp_path / "x.bin.zst"), {"minute_boundary": T0, "sample_rate": FS},
                       20.0, str(fake))
    assert time.monotonic() - t < 10


def test_single_flight_follower_times_out():
    sf = R.SingleFlight()
    go = threading.Event()
    th = threading.Thread(target=lambda: sf.do("k", lambda: go.wait(10)))
    th.start()
    time.sleep(0.1)
    with pytest.raises(R.FlightTimeout):
        sf.do("k", lambda: None, timeout=0.2)
    go.set()
    th.join(10)


@needs_zstd
def test_follower_that_times_out_reports_pending(tmp_path, monkeypatch):
    make_raw(tmp_path / "raw", "WWV_20000", T0, 60, [(0.05, 1.0)])
    started, release = threading.Event(), threading.Event()
    real = R.measure_file

    def slow(*a, **k):
        started.set()
        release.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(R, "measure_file", slow)
    monkeypatch.setattr(R, "FOLLOW_TIMEOUT_S", 0.2)
    w = src(tmp_path)
    th = threading.Thread(target=w.series, args=(20.0, T0, T0 + 600))
    th.start()
    assert started.wait(10)
    s = w.series(20.0, T0, T0 + 600)
    release.set()
    th.join(20)
    assert s["pending"] >= 1 and not s["available"]


def test_read_cap_clamped():
    assert R.read_cap(24000, 600) == 24000 * 660
    assert R.read_cap(10 ** 9, 10 ** 9) == 48000 * (1200 + 60)
    assert R.read_cap(24000, -5) == 24000 * 60


def test_queue_takes_the_most_recent_first(tmp_path, monkeypatch):
    w = src(tmp_path)
    order = []
    monkeypatch.setattr(w, "_measure", lambda ch, mhz, e, f: order.append(e))
    f = {"bin": "", "json": "", "size": 0, "mtime_ns": 0}
    w.enqueue("WWV_20000", 20.0, {T0 - 86400 + 600 * k: f for k in range(3)})   # yesterday
    w.enqueue("WWV_20000", 20.0, {T0 + 600 * k: f for k in range(2)})          # today, later
    w.drain()
    assert order == [T0 + 600, T0, T0 - 86400 + 1200, T0 - 86400 + 600, T0 - 86400]


def test_prune_hourly_even_without_prefill(tmp_path, monkeypatch):
    (tmp_path / "raw" / "WWV_20000").mkdir(parents=True)
    w = src(tmp_path, prefill=False)
    pruned = []
    monkeypatch.setattr(w.store, "prune", lambda before: pruned.append(before))
    t = time.time()
    monkeypatch.setattr(R, "_now", lambda: t)
    w.series(20.0, T0, T0 + 600)
    w.series(20.0, T0, T0 + 600)
    assert len(pruned) == 1 and pruned[0] == pytest.approx(t - R.KEEP_DAYS * 86400)
    monkeypatch.setattr(R, "_now", lambda: t + 3601)
    w.series(20.0, T0, T0 + 600)
    assert len(pruned) == 2


# ---- hf-timestd's F16 archives (archive_dtype = "f16", the default since v3.95) -------

@needs_zstd
def test_f16_chunk_measured_like_complex64(tmp_path):
    make_raw(tmp_path / "raw", "WWV_20000", T0, 120, [(-0.155, 1.0)], dtype="complex-f16")
    s = src(tmp_path).series(20.0, T0, T0 + 600)
    assert s["available"] and [p["t"] for p in s["points"]] == [T0, T0 + 60]
    for p in s["points"]:
        assert abs(p["hz"] - (-0.155)) < 0.005 and p["q"] == "ok"


def test_decimate_stream_f16_matches_complex64():
    rng = np.random.default_rng(7)
    x = (rng.standard_normal(4800) + 1j * rng.standard_normal(4800)).astype("<c8")
    f16 = (x.view("<f4") * 4096.0).astype("<f2").tobytes()
    a = R.decimate_stream(io.BytesIO(x.tobytes()), 240, 4800)
    b = R.decimate_stream(io.BytesIO(f16), 240, 4800, fmt="complex-f16")
    assert len(a) == len(b) == 20
    assert np.allclose(a, b, atol=1e-3)


def test_unknown_dtype_still_refused():
    with pytest.raises(R.CorruptFile, match="unsupported sample format"):
        R.measure_file("/nonexistent", {"dtype": "complex-f8", "sample_rate": FS,
                                        "minute_boundary": T0}, 20.0)


def test_files_refused_for_format_before_f16_support_are_retried(tmp_path):
    cache = tmp_path / "cache"
    st = R.CarrierStore(str(cache))
    with st._use(write=True) as con:
        con.execute("INSERT INTO files VALUES ('WWV_20000', ?, 1, 1, 100, 0, ?, 0, 0, NULL)",
                    (T0, "corrupt: malformed sidecar: unsupported sample format complex-f16/little"))
        con.execute("INSERT INTO files VALUES ('WWV_20000', ?, 1, 1, 100, 0, ?, 0, 0, NULL)",
                    (T0 + 600, "corrupt: zstd: unsupported format"))
        con.commit()
    st2 = R.CarrierStore(str(cache))
    assert set(st2.known("WWV_20000", [T0, T0 + 600])) == {T0 + 600}
