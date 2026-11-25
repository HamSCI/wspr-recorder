"""carrier_browser: read-only, serves only under --root, localhost by default."""
from __future__ import annotations

import http.client
import json
import struct
import threading
from pathlib import Path

import pytest

from wspr_recorder import carrier_browser as cb


def _carrier_wav(meta: dict, n: int = 4) -> bytes:
    comment = json.dumps(meta).encode() + b"\0"
    if len(comment) & 1:
        comment += b"\0"
    info = b"INFO" + b"ICMT" + struct.pack("<I", len(comment)) + comment
    fmt = struct.pack("<HHIIHH", 3, 2, 15, 15 * 8, 8, 32)
    data = b"\0" * (8 * n)
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"LIST" + struct.pack("<I", len(info)) + info
            + b"data" + struct.pack("<I", len(data)) + data)
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "archive"
    d = root / "20261123" / "AC0G-B1" / "20"
    d.mkdir(parents=True)
    (d / "1408_W2_K1ABC_14097100.124.wav").write_bytes(
        _carrier_wav({"message": "K1ABC FN42 37", "snr": -20}))
    (d / "1408_W2_W2XYZ_14097020.500.wav").write_bytes(_carrier_wav({}))
    (d / "1410_W2_K1ABC_14097100.130.wav").write_bytes(_carrier_wav({}))
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "1408_W2_EVIL_14097100.000.wav").write_bytes(
        _carrier_wav({"message": "leaked"}))
    (secret / "passwd.wav").write_bytes(b"top secret")
    return root, secret


@pytest.fixture
def server(tree):
    root, _ = tree
    srv = cb.build_server(root, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _get(srv, path, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1],
                                      timeout=5)
    headers = {"Host": host} if host else {}
    conn.request("GET", path, headers=headers)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, body, r


def _json(srv, path):
    status, body, _ = _get(srv, path)
    assert status == 200, body
    return json.loads(body)


# ---- listing ----------------------------------------------------------------

def test_lists_dates_cycles_spots(server):
    assert _json(server, "/api/dates") == ["20261123"]
    assert _json(server, "/api/cycles?date=20261123") == [
        {"hhmm": "1410", "spots": 1}, {"hhmm": "1408", "spots": 2}]
    spots = _json(server, "/api/spots?date=20261123&hhmm=1408")
    assert [(s["call"], s["mode"], s["band"], s["receiver"]) for s in spots] \
        == [("W2XYZ", "W2", "20", "AC0G-B1"), ("K1ABC", "W2", "20", "AC0G-B1")]
    k1 = spots[1]
    assert k1["rf_hz"] == 14097100.124
    assert k1["meta"]["message"] == "K1ABC FN42 37"
    assert k1["samples"] == 4
    assert k1["path"] == "20261123/AC0G-B1/20/1408_W2_K1ABC_14097100.124.wav"


def test_page_and_file_download(server, tree):
    status, body, r = _get(server, "/")
    assert status == 200 and b"<title>" in body
    assert r.getheader("X-Content-Type-Options") == "nosniff"
    rel = "20261123/AC0G-B1/20/1408_W2_K1ABC_14097100.124.wav"
    status, body, r = _get(server, "/file?path=" + rel)
    assert status == 200 and body[:4] == b"RIFF"
    assert r.getheader("Content-Type") == "audio/wav"


def test_empty_or_missing_root(tmp_path):
    srv = cb.build_server(tmp_path / "missing", "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        assert _json(srv, "/api/dates") == []
        assert not (tmp_path / "missing").exists()      # read-only: not created
    finally:
        srv.shutdown()
        srv.server_close()


# ---- traversal guard --------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/file?path=../secret/passwd.wav",
    "/file?path=20261123/../../secret/passwd.wav",
    "/file?path=%2e%2e/secret/passwd.wav",
    "/file?path=/etc/passwd",
    "/file?path=",
    "/file?path=20261123/AC0G-B1/20",                       # a directory
    "/file?path=20261123/AC0G-B1/20/../../../secret/passwd.wav",
])
def test_file_traversal_refused(server, path):
    status, body, _ = _get(server, path)
    assert status == 404
    assert b"top secret" not in body


def test_symlinked_file_out_of_root_refused(server, tree):
    root, secret = tree
    (root / "20261123" / "AC0G-B1" / "20" / "1412_W2_LINK_1.000.wav").symlink_to(
        secret / "passwd.wav")
    status, body, _ = _get(
        server, "/file?path=20261123/AC0G-B1/20/1412_W2_LINK_1.000.wav")
    assert status == 404 and b"top secret" not in body


def test_symlinked_dirs_out_of_root_not_listed(server, tree):
    root, secret = tree
    (root / "20261123" / "EVILRX").mkdir()
    (root / "20261123" / "EVILRX" / "20").symlink_to(secret,
                                                     target_is_directory=True)
    (root / "20261124").symlink_to(secret, target_is_directory=True)
    assert "20261124" not in _json(server, "/api/dates")
    spots = _json(server, "/api/spots?date=20261123&hhmm=1408")
    assert all(s["receiver"] != "EVILRX" for s in spots)
    assert "leaked" not in json.dumps(spots)


@pytest.mark.parametrize("path", [
    "/api/cycles?date=../secret",
    "/api/spots?date=../secret&hhmm=1408",
    "/api/spots?date=20261123&hhmm=../x",
])
def test_api_params_validated(server, path):
    status, body, _ = _get(server, path)
    assert b"leaked" not in body
    assert status in (200, 400)
    if status == 200:
        assert json.loads(body) == []


def test_write_methods_refused(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                      timeout=5)
    for method in ("POST", "PUT", "DELETE"):
        conn.request(method, "/file?path=x.wav", body=b"x")
        r = conn.getresponse()
        r.read()
        assert r.status in (405, 501)
    conn.close()


def test_foreign_host_header_refused_on_loopback(server):
    """DNS rebinding: a page on evil.example cannot read the archive through
    the victim's browser."""
    status, _, _ = _get(server, "/api/dates", host="evil.example:8089")
    assert status == 403
    for ok in ("localhost:8089", "127.0.0.1:8089", "[::1]:8089"):
        assert _get(server, "/api/dates", host=ok)[0] == 200


# ---- defaults ---------------------------------------------------------------

def test_defaults_bind_localhost_8089(monkeypatch):
    monkeypatch.delenv("CARRIER_BROWSER_BIND", raising=False)
    monkeypatch.delenv("CARRIER_BROWSER_PORT", raising=False)
    a = cb.parse_args(["--root", "/x"])
    assert (a.bind, a.port, a.root) == ("127.0.0.1", 8089, "/x")


def test_python_dash_m_entry_point():
    import runpy
    import sys
    from unittest import mock
    with mock.patch.object(sys, "argv", ["carrier_browser", "--help"]), \
            pytest.raises(SystemExit) as e:
        runpy.run_module("wspr_recorder.carrier_browser", run_name="__main__")
    assert e.value.code == 0


def test_page_escapes_untrusted_text():
    """Calls, messages and receiver names reach innerHTML only escaped."""
    page = cb.PAGE
    assert "function esc(" in page
    html_sinks = [ln for ln in page.splitlines() if "innerHTML=" in ln]
    assert html_sinks
    for ln in html_sinks:
        for raw in ("+s.call+", "+(m.message||\"\")+", "+key+", "+o.label+",
                    "+s.receiver+", "+s.mode+"):
            assert raw not in ln, (raw, ln)


# ---- shipped unit -----------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent


def test_unit_binds_localhost_8089_at_default_root():
    unit = (REPO / "systemd" / "wspr-carrier-browser.service").read_text()
    assert "-m wspr_recorder.carrier_browser" in unit
    assert "CARRIER_ARCHIVE_ROOT=/var/lib/wspr-recorder/carrier-archive" in unit
    assert "CARRIER_BROWSER_BIND=127.0.0.1" in unit
    assert "CARRIER_BROWSER_PORT=8089" in unit
    lines = [ln.strip() for ln in unit.splitlines()]
    assert not any(ln.startswith("ReadWritePaths=") for ln in lines)
    assert "ProtectSystem=strict" in lines
    assert "IPAddressAllow=localhost" in unit


def test_unit_installed_but_never_enabled():
    from wspr_recorder.config import tomllib
    deploy = tomllib.loads((REPO / "deploy.toml").read_text())
    links = [s["dst"] for s in deploy["install"]["steps"] if s["kind"] == "link"]
    assert "/etc/systemd/system/wspr-carrier-browser.service" in links
    assert "wspr-carrier-browser.service" in deploy["systemd"]["optional_units"]
    for f in ("install.sh", "scripts/deploy.sh"):
        text = (REPO / f).read_text()
        assert "enable --now wspr-carrier-browser" not in text
        assert "start wspr-carrier-browser" not in text
