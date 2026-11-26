"""Run the carrier browser page's own JavaScript (under node, stub DOM,
the real /api/compare body) so a runtime error in it fails a test.

The other page tests only grep the HTML; a strict-mode ReferenceError in
loadCompare() (2026-11-26) left the page on "Loading ..." for good.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from wspr_recorder import carrier_browser as cb

from tests.test_ww0wwv_compare import _json, _serve, archive  # noqa: F401

HARNESS = Path(__file__).parent / "js" / "run_page.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def page_script() -> str:
    m = re.search(r"<script>(.*?)</script>", cb.PAGE, re.S)
    assert m, "no <script> in PAGE"
    return m.group(1)


def run(tmp_path, api: dict, fname: str) -> dict:
    js = tmp_path / "page.js"
    js.write_text(page_script())
    (tmp_path / "api.json").write_text(json.dumps(api))
    out = subprocess.run(["node", str(HARNESS), str(js), str(tmp_path / "api.json"), fname],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture
def compare(archive):  # noqa: F811
    srv = _serve(archive)
    try:
        return _json(srv, "/api/compare?date=20261115")
    finally:
        srv.shutdown(); srv.server_close()


@needs_node
def test_load_compare_runs_and_reports(tmp_path, compare):
    r = run(tmp_path, {"/api/compare": compare, "/api/dates": ["20261115"],
                       "__date": "20261115"}, "loadCompare")
    assert r["errors"] == []
    assert not r["status"].startswith("Loading")


@needs_node
def test_load_compare_with_wwv_pending_says_so(tmp_path, compare):
    c = copy.deepcopy(compare)
    c["bands"][0].setdefault("wwv", {})
    c["bands"][0]["wwv"].update({"pending": 5, "channel": "WWV_10000"})
    r = run(tmp_path, {"/api/compare": c, "__date": "20261115"}, "loadCompare")
    assert r["errors"] == []
    assert "5 ten-minute files queued" in r["status"]


@needs_node
def test_init_runs(tmp_path, compare):
    r = run(tmp_path, {"/api/compare": compare, "/api/dates": ["20261115"],
                       "/api/cycles": [], "__date": "20261115"}, "init")
    assert r["errors"] == []
