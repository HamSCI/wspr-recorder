"""The carrier browser's main page: WW0WWV's carrier Doppler against WWV's.

The fixtures are three real WW0WWV carriers that KPH1 decoded on
2026-11-15 at 20:56 UTC (fst4w-freq-test/carriers, byte-identical to the
kph/w*/iq files ww0wwv-cycles.py measured); the expected ppb are that
script's, in fst4w-freq-test/kph/results-kph1.jsonl.
"""
from __future__ import annotations

import http.client
import json
import shutil
import sqlite3
import threading
from pathlib import Path

import numpy as np
import pytest

from wspr_recorder import carrier_browser as cb
from wspr_recorder import ww0wwv as W

FIX = Path(__file__).resolve().parent / "fixtures" / "ww0wwv"
EPOCH_2056 = 1794776160            # 2026-11-15 20:56:00 UTC
DIAL = {"30": 10138700, "20": 14095600, "12": 24924600}
FILES = {"30": "20261115T2056Z_WW0WWV_1449.945.wav",
         "20": "20261115T2056Z_WW0WWV_1449.995.wav",
         "12": "20261115T2056Z_WW0WWV_1449.463.wav"}
# results-kph1.jsonl, cycle 20261115T2056Z, ww0wwv.mean_ppb
REF_PPB = {"30": -18.497, "20": -3.699,
           # this record predates the 12 m correction (no nominal_hz): it
           # was measured against 24926050.049 Hz; day-chart.py shifts it
           # by (24926050.049 - 24926049.919536) / 24926049.919536 * 1e9.
           "12": -25.925 + (24926050.049 - 24926049.919536) / 24926049.919536 * 1e9}


# ---- the physics ----------------------------------------------------------------

def test_band_pairs():
    assert W.BAND_PAIRS == [("80", 2.5), ("40", 5.0), ("30", 10.0), ("20", 15.0), ("15", 20.0),
                            ("12", 25.0), ("10", 25.0)]
    for band, _ in W.BAND_PAIRS:
        assert band in W.NOMINAL_HZ


def test_nominals_from_the_reference_table():
    assert W.NOMINAL_HZ["12"] == 24926049.919536          # the WS-8's, not 24926050.049
    assert W.NOMINAL_HZ["30"] == 10140150.146485
    assert W.NOMINAL_HZ["20"] == 14097050.048829
    assert W.NOMINAL_HZ["15"] == 21096050.048829
    assert W.NOMINAL_HZ["10"] == 28126050.109966


@pytest.mark.parametrize("band", ["30", "20", "12"])
def test_ppb_matches_the_reference_scripts(band):
    meta, rate, iq = W.read_carrier(str(FIX / FILES[band]))
    assert rate == 15 and meta["dial_mhz"] * 1e6 == pytest.approx(DIAL[band])
    s = W.carrier_summary(meta, rate, iq, band)
    assert s["ppb"] == pytest.approx(REF_PPB[band], abs=0.1)
    assert s["nominal_hz"] == W.NOMINAL_HZ[band]


def _tone(offset_hz: float, n: int = 1659, rate: int = 15, noise: float = 0.0) -> np.ndarray:
    t = np.arange(n) / rate
    rng = np.random.default_rng(1)
    return (np.exp(2j * np.pi * offset_hz * t)
            + noise * (rng.standard_normal(n) + 1j * rng.standard_normal(n)))


def test_ppb_of_a_synthetic_carrier():
    nominal = W.NOMINAL_HZ["20"]
    meta = {"dial_mhz": 14.0956, "ref_hz": 1450.0, "snr": -5, "t0_s": 1.75}
    ref = 14.0956e6 + 1450.0
    want_hz = nominal + 0.1 - ref               # RF 0.1 Hz above nominal
    s = W.carrier_summary(meta, 15, _tone(want_hz, noise=0.05), "20")
    assert s["hz"] == pytest.approx(0.1, abs=1e-3)
    assert s["ppb"] == pytest.approx(0.1 / nominal * 1e9, abs=0.1)
    tr = W.carrier_track(meta, 15, _tone(want_hz, noise=0.05), "20", EPOCH_2056)
    assert tr["win_s"] == 10.0
    assert tr["points"][0]["t"] == pytest.approx(EPOCH_2056 + 1.75 + 5.0, abs=0.01)
    assert all(abs(p["ppb"] - s["ppb"]) < 1.0 for p in tr["points"])


def test_weak_signal_uses_30_s_windows():
    meta = {"dial_mhz": 14.0956, "ref_hz": 1450.0, "snr": -20}
    assert W.carrier_track(meta, 15, _tone(0.3), "20", 0)["win_s"] == 30.0


def test_unknown_band_has_no_ppb():
    assert W.carrier_summary({"dial_mhz": 7.0386, "ref_hz": 1500}, 15, _tone(0), "17") is None


# ---- WWV from hf-timestd ----------------------------------------------------------

def make_timestd(path: Path, rows) -> Path:
    """A timestd.db with hf-timestd's L2_tick_timing columns the reader uses."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE L2_tick_timing (timestamp_utc TEXT, minute_boundary_utc INTEGER, "
                "channel TEXT, station TEXT, frequency_mhz REAL, mean_snr_db REAL, "
                "valid_windows INTEGER, total_windows INTEGER, doppler_hz REAL, "
                "doppler_uncertainty_hz REAL)")
    from datetime import datetime, timezone
    con.executemany("INSERT INTO L2_tick_timing (timestamp_utc, minute_boundary_utc, channel, "
                    "station, frequency_mhz, mean_snr_db, doppler_hz) VALUES (?,?,?,?,?,?,?)",
                    [(datetime.fromtimestamp(r[0] + 62, timezone.utc).isoformat(), *r) for r in rows])
    con.commit()
    con.close()
    return path


def test_timestd_series(tmp_path):
    db = make_timestd(tmp_path / "timestd.db", [
        (EPOCH_2056, "SHARED_10000", "WWV", 10.0, 12.0, -0.1),       # -10 ppb
        (EPOCH_2056 + 60, "SHARED_10000", "WWV", 10.0, 11.0, -0.12),  # -12 ppb
        (EPOCH_2056 + 60, "SHARED_10000", "WWVH", 10.0, 3.0, 0.5),    # not WWV
        (EPOCH_2056 + 120, "SHARED_10000", "WWV", 10.0, 3.0, None),   # no Doppler
        (EPOCH_2056, "WWV_20000", "WWV", 20.0, 9.0, 0.02),
    ])
    s = W.TimestdWwv(str(db)).series(10.0, EPOCH_2056 - 600, EPOCH_2056 + 600)
    assert s["available"] and s["channel"] == "SHARED_10000"
    assert [(p["t"], p["ppb"], p["snr"]) for p in s["points"]] == [
        (EPOCH_2056, -10.0, 12.0), (EPOCH_2056 + 60, -12.0, 11.0)]
    assert W.wwv_over(s, EPOCH_2056, EPOCH_2056 + 120) == -11.0
    s20 = W.TimestdWwv(str(db)).series(20.0, EPOCH_2056, EPOCH_2056 + 60)
    assert s20["channel"] == "WWV_20000" and s20["points"][0]["ppb"] == 1.0
    none = W.TimestdWwv(str(db)).series(25.0, EPOCH_2056, EPOCH_2056 + 60)
    assert not none["available"] and "25 MHz" in none["reason"]


def test_timestd_missing_or_foreign_db(tmp_path):
    s = W.TimestdWwv(str(tmp_path / "nope.db")).series(10.0, 0, 60)
    assert not s["available"] and "no hf-timestd database" in s["reason"]
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x (a)").connection.commit()
    s = W.TimestdWwv(str(other)).series(10.0, 0, 60)
    assert not s["available"] and "L2_tick_timing" in s["reason"]


def test_timestd_opened_read_only(tmp_path):
    db = make_timestd(tmp_path / "timestd.db", [(EPOCH_2056, "WWV_10000", "WWV", 10.0, 1.0, 0.0)])
    db.chmod(0o444)
    tmp_path.chmod(0o555)          # no -journal / -wal could be created
    try:
        assert W.TimestdWwv(str(db)).series(10.0, 0, 2e9)["available"]
    finally:
        tmp_path.chmod(0o755)
        db.chmod(0o644)


# ---- the endpoints --------------------------------------------------------------

@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "archive"
    for band, fn in FILES.items():
        d = root / "20261115" / "KPH1" / band
        d.mkdir(parents=True)
        rf = DIAL[band] + float(fn.rsplit("_", 1)[1][:-4])
        shutil.copy(FIX / fn, d / f"2056_W2_WW0WWV_{rf:.3f}.wav")
    # another call and an FST4W carrier of WW0WWV: not on the main page
    shutil.copy(FIX / FILES["30"], root / "20261115" / "KPH1" / "30" / "2056_W2_K1ABC_10140100.000.wav")
    shutil.copy(FIX / FILES["30"], root / "20261115" / "KPH1" / "30" / "2056_F2_WW0WWV_10140100.000.wav")
    return root


def _serve(root, **kw):
    srv = cb.build_server(root, kw.pop("bind", "127.0.0.1"), 0, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _get(srv, path, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("GET", path, headers={"Host": host} if host else {})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, body


def _json(srv, path):
    status, body = _get(srv, path)
    assert status == 200, body
    return json.loads(body)


def test_compare_without_wwv(archive):
    srv = _serve(archive)
    try:
        d = _json(srv, "/api/compare?date=20261115")
    finally:
        srv.shutdown(); srv.server_close()
    assert d["call"] == "WW0WWV" and d["receivers"] == ["KPH1"]
    assert [(b["band"], b["wwv_mhz"]) for b in d["bands"]] == W.BAND_PAIRS
    by = {b["band"]: b for b in d["bands"]}
    for band in FILES:
        (c,) = by[band]["cycles"]
        assert c["hhmm"] == "2056" and c["epoch"] == EPOCH_2056
        assert c["ppb"] == pytest.approx(REF_PPB[band], abs=0.1)
        assert c["snr_wsprd"] is not None and c["wwv_ppb"] is None
        assert by[band]["wwv"]["available"] is False
        assert by[band]["wwv"]["reason"]
    assert by["15"]["cycles"] == [] and by["10"]["cycles"] == []


def test_compare_and_cycle_with_wwv(archive, tmp_path):
    db = make_timestd(tmp_path / "timestd.db", [
        (EPOCH_2056 + k, "SHARED_10000", "WWV", 10.0, 5.0, -0.13) for k in (-60, 0, 60, 120)])
    srv = _serve(archive, wwv=W.TimestdWwv(str(db)))
    try:
        d = _json(srv, "/api/compare?date=20261115")
        c = _json(srv, "/api/cycle?date=20261115&hhmm=2056&band=30")
        miss = _get(srv, "/api/cycle?date=20261115&hhmm=2058&band=30")[0]
        bad = _get(srv, "/api/cycle?date=20261115&hhmm=2056&band=../x")[0]
        bad_date = _get(srv, "/api/compare?date=../x")[0]
    finally:
        srv.shutdown(); srv.server_close()
    b30 = next(b for b in d["bands"] if b["band"] == "30")
    assert b30["wwv"]["available"] and b30["wwv"]["channel"] == "SHARED_10000"
    assert len(b30["wwv"]["points"]) == 4
    assert b30["cycles"][0]["wwv_ppb"] == pytest.approx(-13.0)
    assert not next(b for b in d["bands"] if b["band"] == "20")["wwv"]["available"]
    assert c["band"] == "30" and c["wwv_mhz"] == 10.0 and c["epoch"] == EPOCH_2056
    assert c["summary"]["ppb"] == pytest.approx(REF_PPB["30"], abs=0.1)
    assert c["wwv_ppb"] == pytest.approx(-13.0)
    assert c["track"]["win_s"] == 10.0 and len(c["track"]["points"]) > 40
    assert {p["t"] for p in c["wwv"]["points"]} == {EPOCH_2056 + k for k in (-60, 0, 60, 120)}
    assert c["path"] == "20261115/KPH1/30/2056_W2_WW0WWV_10140149.945.wav"
    assert (miss, bad, bad_date) == (404, 400, 400)


def test_compare_json_has_no_nan(archive):
    srv = _serve(archive)
    try:
        status, body = _get(srv, "/api/compare?date=20261115")
    finally:
        srv.shutdown(); srv.server_close()
    assert status == 200 and b"NaN" not in body and b"Infinity" not in body


def test_page_is_the_comparison():
    page = cb.PAGE
    assert "<title>WW0WWV vs WWV</title>" in page
    assert "/api/compare" in page and "/api/cycle" in page
    assert "All spots" in page
    assert "no WWV data" in page
    assert "<script src" not in page and "https://" not in page      # nothing from a CDN


# ---- bind and Host --------------------------------------------------------------

@pytest.mark.parametrize("host, ok", [
    ("localhost:8089", True), ("127.0.0.1:8089", True), ("[::1]:8089", True),
    ("10.98.0.5:8089", True), ("44.1.2.3", True), ("[fe80::1]:8089", True),
    ("relay.example:8089", True), ("RELAY.example.", True),
    ("evil.example:8089", False), ("", False), ("10.98.0.5.evil.example", False),
])
def test_host_ok(host, ok):
    allowed = cb.allowed_hosts("0.0.0.0", ["relay.example"])
    assert cb.host_ok(host, allowed) is ok


def test_bound_to_all_addresses_still_blocks_rebinding(tmp_path, monkeypatch):
    import socket
    monkeypatch.setenv(cb.ENV_ALLOWED, "kph-main.example, other.example")
    srv = _serve(tmp_path, bind="0.0.0.0")
    try:
        assert not srv.loopback_only
        port = srv.server_address[1]
        assert _get(srv, "/api/dates", host=f"evil.example:{port}")[0] == 403
        for ok in (f"127.0.0.1:{port}", f"localhost:{port}", f"10.98.0.7:{port}",
                   f"kph-main.example:{port}", "other.example",
                   socket.gethostname()):
            assert _get(srv, "/api/dates", host=ok)[0] == 200, ok
    finally:
        srv.shutdown(); srv.server_close()


def test_bind_default_from_env(monkeypatch):
    monkeypatch.delenv(cb.ENV_BIND, raising=False)
    monkeypatch.delenv(cb.ENV_PORT, raising=False)
    a = cb.parse_args(["--root", "/x"])
    assert (a.bind, a.port) == ("127.0.0.1", 8089)
    monkeypatch.setenv(cb.ENV_BIND, "0.0.0.0")
    monkeypatch.setenv(cb.ENV_PORT, "8090")
    a = cb.parse_args(["--root", "/x"])
    assert (a.bind, a.port) == ("0.0.0.0", 8090)
    assert cb.parse_args(["--root", "/x", "--bind", "10.98.0.5"]).bind == "10.98.0.5"


def test_timestd_db_default_and_none(monkeypatch):
    monkeypatch.delenv(cb.ENV_TIMESTD_DB, raising=False)
    assert cb.parse_args(["--root", "/x"]).timestd_db == "/var/lib/timestd/phase2/timestd.db"
    monkeypatch.setenv(cb.ENV_TIMESTD_DB, "none")
    assert cb.parse_args(["--root", "/x"]).timestd_db == "none"


def test_unit_takes_bind_from_env():
    unit = (Path(__file__).resolve().parent.parent / "systemd"
            / "wspr-carrier-browser.service").read_text()
    assert "Environment=CARRIER_BROWSER_BIND=127.0.0.1" in unit
    assert "Environment=TIMESTD_DB=/var/lib/timestd/phase2/timestd.db" in unit
    assert "Environment=CARRIER_BROWSER_ALLOWED_HOSTS=" in unit
    assert "IPAddressDeny=any" in unit
