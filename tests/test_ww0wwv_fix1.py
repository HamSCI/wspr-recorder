"""Fix round 1 for the WW0WWV carrier browser: WWV alias range, bad carrier
files, resource bounds, the indexed WWV query, the archive's new default."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sqlite3
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from wspr_recorder import carrier_browser as cb
from wspr_recorder import ww0wwv as W

FIX = Path(__file__).resolve().parent / "fixtures" / "ww0wwv"
REPO = Path(__file__).resolve().parent.parent
EPOCH = 1794776160                 # 2026-11-15 20:56:00 UTC
F30 = "20261115T2056Z_WW0WWV_1449.945.wav"
F12 = "20261115T2056Z_WW0WWV_1449.463.wav"


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def make_timestd(path: Path, rows, wal=False) -> Path:
    """hf-timestd's L2_tick_timing as hamsci-dsp's writer makes it, with its
    (channel, timestamp_utc) index; timestamp_utc is the write time, a few
    seconds after the minute."""
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE L2_tick_timing (channel TEXT NOT NULL, timestamp_utc TEXT NOT NULL, "
                "minute_boundary_utc INTEGER NOT NULL, station TEXT NOT NULL, frequency_mhz REAL, "
                "mean_snr_db REAL, doppler_hz REAL)")
    con.execute("CREATE INDEX idx_L2_tick_timing_chan_ts ON L2_tick_timing (channel, timestamp_utc)")
    con.executemany("INSERT INTO L2_tick_timing VALUES (?,?,?,?,?,?,?)",
                    [(ch, iso(t + 63), t, st, mhz, snr, hz) for t, ch, st, mhz, snr, hz in rows])
    con.commit()
    con.close()
    return path


def write_carrier(path: Path, meta: dict, iq: np.ndarray, rate: int = 15):
    comment = json.dumps(meta).encode() + b"\0"
    if len(comment) & 1:
        comment += b"\0"
    info = b"INFO" + b"ICMT" + struct.pack("<I", len(comment)) + comment
    fmt = struct.pack("<HHIIHH", 3, 2, rate, rate * 8, 8, 32)
    a = np.empty(2 * len(iq), dtype="<f4")
    a[0::2], a[1::2] = iq.real, iq.imag
    data = a.tobytes()
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"LIST" + struct.pack("<I", len(info)) + info
            + b"data" + struct.pack("<I", len(data)) + data)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "archive"
    for band, fn, dial in (("30", F30, 10138700), ("12", F12, 24924600)):
        d = root / "20261115" / "KPH1" / band
        d.mkdir(parents=True)
        rf = dial + float(fn.rsplit("_", 1)[1][:-4])
        shutil.copy(FIX / fn, d / f"2056_W2_WW0WWV_{rf:.3f}.wav")
    return root


def _serve(root, **kw):
    srv = cb.build_server(root, "127.0.0.1", 0, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _get(srv, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, body


def _stop(srv):
    srv.shutdown()
    srv.server_close()


# ---- 1. WWV's alias range -----------------------------------------------------------

def test_wwv_limit_ppb():
    assert W.WWV_DOPPLER_LIMIT_HZ == 0.5
    assert W.wwv_limit_ppb(10.0) == pytest.approx(50.0)
    assert W.wwv_limit_ppb(15.0) == pytest.approx(33.333, abs=1e-3)
    assert W.wwv_limit_ppb(20.0) == pytest.approx(25.0)
    assert W.wwv_limit_ppb(25.0) == pytest.approx(20.0)


@pytest.mark.parametrize("ww0, wwv_ppb, mhz, aliased", [
    (-18.4, -13.0, 10.0, False),
    (-20.7, -10.0, 25.0, True),        # WW0WWV beyond 25 MHz's +-20 ppb
    (-10.0, -47.0, 10.0, True),        # WWV within 10 % of the 0.5 Hz edge
    (-10.0, -44.0, 10.0, False),
    (-10.0, None, 10.0, False),        # no WWV: nothing to alias
])
def test_may_alias(ww0, wwv_ppb, mhz, aliased):
    assert W.wwv_may_alias(ww0, wwv_ppb, mhz) is aliased


def test_compare_flags_aliased_cycles(archive, tmp_path):
    db = make_timestd(tmp_path / "timestd.db", [
        (EPOCH + k, "SHARED_10000", "WWV", 10.0, 5.0, -0.13) for k in (0, 60)] + [
        (EPOCH + k, "WWV_25000", "WWV", 25.0, 5.0, -0.25) for k in (0, 60)])
    srv = _serve(archive, wwv=W.TimestdWwv(str(db)))
    try:
        st, body = _get(srv, "/api/compare?date=20261115")
        st2, body2 = _get(srv, "/api/cycle?date=20261115&hhmm=2056&band=12")
    finally:
        _stop(srv)
    assert st == 200 and st2 == 200
    by = {b["band"]: b for b in json.loads(body)["bands"]}
    assert by["30"]["wwv_limit_ppb"] == pytest.approx(50.0)
    assert by["12"]["wwv_limit_ppb"] == pytest.approx(20.0)
    assert by["10"]["wwv_limit_ppb"] == pytest.approx(20.0)
    c30, c12 = by["30"]["cycles"][0], by["12"]["cycles"][0]
    assert c30["wwv_alias"] is False and c30["diff_ppb"] == pytest.approx(c30["ppb"] + 13.0)
    # 12 m: WW0WWV -20.7 ppb is past what WWV 25 MHz resolves (+-20 ppb)
    assert c12["wwv_alias"] is True and c12["diff_ppb"] is None
    assert c12["wwv_ppb"] == pytest.approx(-10.0)
    d = json.loads(body2)
    assert d["wwv_alias"] is True and d["diff_ppb"] is None and d["wwv_limit_ppb"] == pytest.approx(20.0)


def test_page_says_may_be_aliased():
    assert "WWV may be aliased" in cb.PAGE
    assert "wwv_limit_ppb" in cb.PAGE


def test_docs_no_longer_call_it_a_residual():
    doc = (REPO / "docs" / "CARRIER-ARCHIVE.md").read_text()
    assert "residual" not in doc
    assert "alias" in doc


# ---- 2. a bad carrier file never drops the connection ------------------------------

def test_bad_t0_gives_json_error_not_a_dropped_connection(archive):
    d = archive / "20261115" / "KPH1" / "30"
    meta, rate, iq = W.read_carrier(str(FIX / F30))
    meta["t0_s"] = "not a number"
    write_carrier(d / "2058_W2_WW0WWV_10140150.000.wav", meta, iq, rate)
    srv = _serve(archive)
    try:
        st, body = _get(srv, "/api/cycle?date=20261115&hhmm=2058&band=30")
        st_ok, _ = _get(srv, "/api/compare?date=20261115")
    finally:
        _stop(srv)
    assert 400 <= st < 600 and "error" in json.loads(body)
    assert st_ok == 200


def test_summary_failure_skips_the_file_and_logs_once(archive, monkeypatch, caplog):
    real = W.carrier_summary

    def boom(meta, rate, iq, band, *a, **k):
        if band == "12":
            raise TypeError("malformed")
        return real(meta, rate, iq, band, *a, **k)
    monkeypatch.setattr(W, "carrier_summary", boom)
    srv = _serve(archive)
    try:
        caplog.set_level("WARNING", logger="wspr_recorder.carrier_browser")
        st, body = _get(srv, "/api/compare?date=20261115")
        srv.cache.clear()
        st2, _ = _get(srv, "/api/compare?date=20261115")
    finally:
        _stop(srv)
    assert st == 200 and st2 == 200
    by = {b["band"]: b for b in json.loads(body)["bands"]}
    assert by["12"]["cycles"] == [] and len(by["30"]["cycles"]) == 1
    assert sum("2056_W2_WW0WWV_24926049.463.wav" in r.getMessage() for r in caplog.records) == 1


def test_unexpected_error_is_a_500_json(archive, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(cb, "dates", boom)
    srv = _serve(archive)
    try:
        st, body = _get(srv, "/api/dates")
    finally:
        _stop(srv)
    assert st == 500 and json.loads(body)["error"]


# ---- 3. resource bounds --------------------------------------------------------------

def test_single_flight_runs_once():
    sf = cb.SingleFlight()
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.3)
        return {"n": len(calls)}
    out = []
    ts = [threading.Thread(target=lambda: out.append(sf.do("20261115", slow))) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) == 1 and out == [{"n": 1}] * 6
    sf.do("20261115", slow)                         # finished: a later call runs again
    assert len(calls) == 2


def test_concurrent_compare_requests_compute_once(archive, monkeypatch):
    n = []
    real = cb.compare

    def counted(*a, **k):
        n.append(1)
        time.sleep(0.3)
        return real(*a, **k)
    monkeypatch.setattr(cb, "compare", counted)
    srv = _serve(archive)
    res = []
    try:
        ts = [threading.Thread(target=lambda: res.append(_get(srv, "/api/compare?date=20261115")[0]))
              for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    finally:
        _stop(srv)
    assert res == [200] * 4 and len(n) == 1


def test_cache_is_bounded_lru(tmp_path):
    c = cb.CarrierCache(limits={"summary": 3, "track": 1})
    files = []
    for i in range(5):
        p = tmp_path / f"{i}.wav"
        p.write_bytes(b"x")
        files.append(str(p))
        c.get(str(p), "summary", lambda i=i: {"i": i})
        c.get(str(p), "track", lambda i=i: {"t": i})
    assert c.size("summary") == 3 and c.size("track") == 1
    hits = []
    assert c.get(files[4], "summary", lambda: hits.append(1)) == {"i": 4} and not hits
    assert cb.CarrierCache().limits["track"] <= 500     # a full walk stays small


def test_busy_server_answers_503(archive):
    srv = _serve(archive)
    srv.slots = threading.BoundedSemaphore(1)
    srv.slots.acquire()                       # every slot taken
    try:
        st, body = _get(srv, "/api/dates")
    finally:
        srv.slots.release()
        _stop(srv)
    assert st == 503 and json.loads(body)["error"] == "busy"


def test_unit_caps_cpu():
    unit = (REPO / "systemd" / "wspr-carrier-browser.service").read_text()
    assert "CPUQuota=50%" in [ln.strip() for ln in unit.splitlines()]


# ---- 4. the WWV query ------------------------------------------------------------------

def test_wwv_query_uses_the_timestamp_index(tmp_path):
    db = make_timestd(tmp_path / "timestd.db", [(EPOCH, "WWV_10000", "WWV", 10.0, 1.0, -0.1)])
    w = W.TimestdWwv(str(db))
    sql, args = w.query_sql(10.0, EPOCH - 60, EPOCH + 120)
    plan = " ".join(str(r) for r in sqlite3.connect(db).execute("EXPLAIN QUERY PLAN " + sql, args))
    assert "idx_L2_tick_timing_chan_ts" in plan and "timestamp_utc" in plan
    s = w.series(10.0, EPOCH - 60, EPOCH + 120)
    assert [p["t"] for p in s["points"]] == [EPOCH]
    # a row written late (writer catching up) is still found
    late = make_timestd(tmp_path / "late.db", [(EPOCH, "WWV_10000", "WWV", 10.0, 1.0, -0.1)])
    con = sqlite3.connect(late)
    con.execute("UPDATE L2_tick_timing SET timestamp_utc = ?", (iso(EPOCH + 600),))
    con.commit()
    con.close()
    assert W.TimestdWwv(str(late)).series(10.0, EPOCH, EPOCH + 120)["available"]


def test_wwv_writer_stopped_says_not_running(tmp_path):
    d = tmp_path / "phase2"
    d.mkdir()
    db = make_timestd(d / "timestd.db", [(EPOCH, "WWV_10000", "WWV", 10.0, 1.0, -0.1)], wal=True)
    assert not os.path.exists(str(db) + "-shm")      # writer closed: WAL files gone
    d.chmod(0o555)                                   # and we cannot create them
    try:
        s = W.TimestdWwv(str(db)).series(10.0, EPOCH, EPOCH + 120)
    finally:
        d.chmod(0o755)
    assert not s["available"] and "hf-timestd not running" in s["reason"]


# ---- 5. docs --------------------------------------------------------------------------

def test_docs_say_enabling_keeps_only_ww0wwv():
    for f in ("docs/CARRIER-ARCHIVE.md", "docs/CONFIG.md", "config.toml.example"):
        text = (REPO / f).read_text()
        assert "only WW0WWV" in text, f
