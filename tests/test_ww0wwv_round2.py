"""Round 2 of the WW0WWV carrier browser: Hz offsets beside ppb, and
absolute signal levels from the station's calibrated noise rows."""
from __future__ import annotations

import http.client
import json
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wspr_recorder import carrier_browser as cb
from wspr_recorder import ww0wwv as W
from wspr_recorder.wav_writer import source_slug

FIX = Path(__file__).resolve().parent / "fixtures" / "ww0wwv"
EPOCH = 1794776160                 # 2026-11-15 20:56:00 UTC
F30 = "20261115T2056Z_WW0WWV_1449.945.wav"     # wsprd SNR -8
F20 = "20261115T2056Z_WW0WWV_1449.995.wav"     # wsprd SNR +1
TEN_LOG_2500 = 33.979400086720375

# hamsci_sink's queue, as sigmond's PENDING_UPLOADS_DDL builds it
QUEUE_DDL = """
CREATE TABLE pending_uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, target_db TEXT NOT NULL,
    target_table TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL, queued_at TEXT NOT NULL,
    producer TEXT NOT NULL DEFAULT '', local INTEGER NOT NULL DEFAULT 0);
CREATE INDEX idx_pending_uploads_target ON pending_uploads (target_db, target_table, id);
CREATE INDEX idx_pending_uploads_cycle_time ON pending_uploads (target_db, target_table,
    json_extract(payload_json, '$.time'));
"""


def noise_row(t, band, reporter, fft, rms=-140.0, rx_source="KPH1"):
    """A wspr.noise payload as spot_sink.noise_to_row writes it."""
    return {"time": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "band": band, "radiod_id": "kph1", "rx_source": rx_source,
            "reporter_id": reporter, "host_id": "h", "rx_call": "KPH", "rx_grid": "CM88",
            "rms_noise_dbm": rms, "fft_noise_dbm": fft, "overload_count": 0,
            "schema_version": 1, "uploaded_at": None}


def make_sink(path: Path, rows, target_db="wspr") -> Path:
    con = sqlite3.connect(path)
    con.executescript(QUEUE_DDL)
    con.executemany("INSERT INTO pending_uploads (target_db, target_table, schema_version, "
                    "payload_json, queued_at) VALUES (?, ?, 1, ?, '')",
                    [(target_db, tbl, json.dumps(r)) for tbl, r in rows])
    con.commit()
    con.close()
    return path


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "archive"
    for band, fn, dial in (("30", F30, 10138700), ("20", F20, 14095600)):
        d = root / "20261115" / "KPH1" / band
        d.mkdir(parents=True)
        rf = dial + float(fn.rsplit("_", 1)[1][:-4])
        shutil.copy(FIX / fn, d / f"2056_W2_WW0WWV_{rf:.3f}.wav")
    return root


def _serve(root, **kw):
    srv = cb.build_server(root, "127.0.0.1", 0, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _json(srv, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    assert r.status == 200, body
    return json.loads(body)


def _both(root, **kw):
    srv = _serve(root, **kw)
    try:
        return (_json(srv, "/api/compare?date=20261115"),
                _json(srv, "/api/cycle?date=20261115&hhmm=2056&band=30"))
    finally:
        srv.shutdown()
        srv.server_close()


# ---- 1. Hz beside ppb -----------------------------------------------------------------

def test_hz_and_ppb_in_both_endpoints(archive, tmp_path):
    from tests.test_ww0wwv_fix1 import make_timestd
    db = make_timestd(tmp_path / "timestd.db", [
        (EPOCH + k, "SHARED_10000", "WWV", 10.0, 5.0, -0.13) for k in (0, 60)])
    d, c = _both(archive, wwv=W.TimestdWwv(str(db)))
    b30 = next(b for b in d["bands"] if b["band"] == "30")
    assert b30["nominal_hz"] == W.NOMINAL_HZ["30"]
    (cy,) = b30["cycles"]
    # WW0WWV: Hz from its own nominal (RF - nominal), ppb of it
    assert cy["hz"] == pytest.approx(cy["ppb"] * W.NOMINAL_HZ["30"] / 1e9, abs=1e-5)
    assert cy["hz"] == pytest.approx(-0.187, abs=0.002)
    # WWV: hf-timestd's doppler_hz as is, and its ppb
    assert cy["wwv_hz"] == pytest.approx(-0.13) and cy["wwv_ppb"] == pytest.approx(-13.0)
    assert [(p["hz"], p["ppb"]) for p in b30["wwv"]["points"]] == [(-0.13, -13.0)] * 2
    assert b30["wwv_limit_hz"] == 0.5
    # the drill-down: whole transmission, every track window, WWV minutes
    assert c["summary"]["hz"] == pytest.approx(cy["hz"], abs=1e-6)
    pts = c["track"]["points"]
    assert all(p["hz"] == pytest.approx(p["ppb"] * W.NOMINAL_HZ["30"] / 1e9, abs=1e-5) for p in pts)
    assert c["wwv_hz"] == pytest.approx(-0.13)


def test_track_hz_from_synthetic_carrier():
    import numpy as np
    nominal = W.NOMINAL_HZ["20"]
    meta = {"dial_mhz": 14.0956, "ref_hz": 1450.0, "snr": 0}
    off = nominal + 0.2 - (14.0956e6 + 1450.0)
    iq = np.exp(2j * np.pi * off * np.arange(1659) / 15)
    tr = W.carrier_track(meta, 15, iq, "20", 0)
    assert all(p["hz"] == pytest.approx(0.2, abs=2e-3) for p in tr["points"])


def test_page_has_unit_toggle():
    page = cb.PAGE
    assert 'id="uPpb"' in page and 'id="uHz"' in page
    assert "Hz from nominal" in page


# ---- 2. absolute levels ---------------------------------------------------------------

def test_signal_dbm_from_snr_and_noise():
    # wsprd's SNR is in 2500 Hz; the noise rows are dBm in 1 Hz
    assert W.signal_dbm(-8, -150.0) == pytest.approx(-8 - 150.0 + TEN_LOG_2500)
    assert W.noise_in_2500_dbm(-150.0) == pytest.approx(-150.0 + TEN_LOG_2500)
    assert W.signal_dbm(None, -150.0) is None and W.signal_dbm(-8, None) is None


def test_compare_levels_from_sink_noise(archive, tmp_path):
    sink = make_sink(tmp_path / "sink.db", [
        ("noise", noise_row(EPOCH, "30", "KPH1", fft=-150.0)),
        ("noise", noise_row(EPOCH, "20", "KPH1", fft=0.0, rms=-145.0)),   # no FFT: RMS
        ("noise", noise_row(EPOCH, "30", "OTHER", fft=-120.0)),            # other receiver
        ("noise", noise_row(EPOCH + 120, "30", "KPH1", fft=-151.0)),       # other cycle
        ("spots", noise_row(EPOCH, "30", "KPH1", fft=-99.0)),              # not noise
    ])
    d, c = _both(archive, noise=cb.SinkNoise(str(sink)))
    by = {b["band"]: b for b in d["bands"]}
    c30, c20 = by["30"]["cycles"][0], by["20"]["cycles"][0]
    assert c30["noise_dbm_hz"] == -150.0 and c30["noise_kind"] == "fft"
    assert c30["level_dbm"] == pytest.approx(-8 - 150.0 + TEN_LOG_2500, abs=0.01)
    assert c20["noise_dbm_hz"] == -145.0 and c20["noise_kind"] == "rms"
    assert c20["level_dbm"] == pytest.approx(1 - 145.0 + TEN_LOG_2500, abs=0.01)
    assert d["levels"]["available"] is True and "wspr-recorder" in d["levels"]["source"]
    assert "no calibrated" in d["levels"]["wwv"]
    # drill-down: the cycle's level and a per-window level track
    assert c["level_dbm"] == pytest.approx(c30["level_dbm"])
    assert c["noise_dbm_hz"] == -150.0
    for p in c["track"]["points"]:
        assert p["level_dbm"] == pytest.approx(p["snr"] - 150.0 + TEN_LOG_2500, abs=0.01)


def test_levels_without_sink(archive, tmp_path):
    d, c = _both(archive, noise=cb.SinkNoise(str(tmp_path / "missing.db")))
    assert d["levels"]["available"] is False and "sink" in d["levels"]["reason"]
    cy = next(b for b in d["bands"] if b["cycles"])["cycles"][0]
    assert cy["level_dbm"] is None and cy["noise_dbm_hz"] is None
    assert c["level_dbm"] is None
    assert all(p.get("level_dbm") is None for p in c["track"]["points"])


def test_levels_default_off_in_tests(archive):
    d, _ = _both(archive)
    assert d["levels"]["available"] is False


def test_multi_source_receiver_matches_noise_rows(tmp_path):
    key = "radiod:bee1-status.local"
    rx_dir = cb.safe_name(f"AC0G/B4_{source_slug(key)}")
    sink = make_sink(tmp_path / "sink.db", [
        ("noise", noise_row(EPOCH, "20", "AC0G/B4", fft=-149.0, rx_source=key)),
        ("noise", noise_row(EPOCH, "20", "AC0G/B4", fft=-139.0,
                            rx_source="radiod:bee2-status.local")),
    ])
    n = cb.SinkNoise(str(sink)).day(EPOCH - 3600, EPOCH + 3600)
    assert n["rows"][(rx_dir, "20", EPOCH)]["dbm_hz"] == -149.0
    assert n["rows"][(cb.safe_name("AC0G/B4"), "20", EPOCH)]["dbm_hz"] in (-149.0, -139.0)


def test_sink_noise_uses_the_cycle_time_index(tmp_path):
    sink = make_sink(tmp_path / "sink.db", [("noise", noise_row(EPOCH, "30", "KPH1", -150.0))])
    sql, args = cb.SinkNoise(str(sink)).query_sql(EPOCH, EPOCH + 120)
    plan = " ".join(str(r) for r in sqlite3.connect(sink).execute("EXPLAIN QUERY PLAN " + sql, args))
    assert "idx_pending_uploads_cycle_time" in plan


def test_sink_alias_from_env(tmp_path, monkeypatch):
    sink = make_sink(tmp_path / "sink.db", [("noise", noise_row(EPOCH, "30", "KPH1", -150.0))],
                     target_db="wspr_kph")
    monkeypatch.setenv("SIGMOND_SQLITE_DB_WSPR", "wspr_kph")
    n = cb.SinkNoise(str(sink)).day(EPOCH, EPOCH + 120)
    assert n["available"] and n["rows"][("KPH1", "30", EPOCH)]["dbm_hz"] == -150.0


def test_sink_path_resolution(tmp_path, monkeypatch):
    for v in ("CARRIER_BROWSER_SINK_DB", "HAMSCI_SINK_PATH", "SIGMOND_SQLITE_PATH"):
        monkeypatch.delenv(v, raising=False)
    toml = tmp_path / "sink.toml"
    toml.write_text('path = "/srv/sink/sink.db"\n')
    legacy = tmp_path / "legacy.db"
    assert cb.resolve_sink_path(config_file=str(toml), legacy=str(legacy)) == "/srv/sink/sink.db"
    legacy.write_bytes(b"")
    assert cb.resolve_sink_path(config_file=str(tmp_path / "none.toml"),
                                legacy=str(legacy)) == str(legacy)
    monkeypatch.setenv("SIGMOND_SQLITE_PATH", "/x/sink.db")
    assert cb.resolve_sink_path(config_file=str(toml), legacy=str(legacy)) == "/x/sink.db"
    monkeypatch.setenv("CARRIER_BROWSER_SINK_DB", "none")
    assert cb.resolve_sink_path(config_file=str(toml), legacy=str(legacy)) == "none"


def test_page_labels_levels_and_calibration():
    page = cb.PAGE
    assert "dBm" in page and "level_dbm" in page
    assert "noise rows" in page               # names the calibration source
