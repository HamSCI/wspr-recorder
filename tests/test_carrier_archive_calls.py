"""[carrier_archive] calls: during the beta only WW0WWV's carriers are kept."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from wspr_recorder.carrier_archive import CarrierArchive, base_call, call_filter
from wspr_recorder.config import CarrierArchiveConfig, load_config

WHEN = datetime(2026, 11, 23, 14, 8, tzinfo=timezone.utc)

BASE = """
[radiod]
status = "test-status.local"

[[band]]
frequency = "14095600"
modes = ["W2"]
"""


def _scratch(arch: CarrierArchive, tmp_path: Path, *names: str) -> Path:
    d = arch.scratch_dir(tmp_path / "work", "p1")
    for n in names:
        (d / n).write_bytes(b"RIFF")
    return d


def _filed(root: Path):
    return sorted(p.name for p in root.rglob("*.wav"))


def _arch(tmp_path, calls):
    cfg = CarrierArchiveConfig(enabled=True, root=str(tmp_path / "archive"),
                               calls=calls)
    return CarrierArchive.from_config(cfg, "AC0G-B1")


def test_default_is_ww0wwv_only():
    assert CarrierArchiveConfig().calls == ["WW0WWV"]
    # each instance its own list
    a, b = CarrierArchiveConfig(), CarrierArchiveConfig()
    a.calls.append("K1ABC")
    assert b.calls == ["WW0WWV"]


def test_ww0wwv_filed_k1abc_discarded_no_scratch_left(tmp_path):
    arch = _arch(tmp_path, ["WW0WWV"])
    src = _scratch(arch, tmp_path, "WW0WWV_1449.945.wav", "K1ABC_1512.351.wav",
                   "WW0WWV_P_1450.100.wav", "ww0wwv_1451.000.wav")
    n = arch.file_cycle([src], band="30", when=WHEN, mode="W2",
                        dial_hz=10138700)
    assert n == 3
    assert _filed(tmp_path / "archive") == [
        "1408_W2_WW0WWV_10140149.945.wav",
        "1408_W2_WW0WWV_P_10140150.100.wav",
        "1408_W2_ww0wwv_10140151.000.wav",
    ]
    assert not src.exists()
    scratch = tmp_path / "work" / CarrierArchive.SCRATCH_DIR
    assert list(scratch.iterdir()) == []


def test_only_unwanted_calls_leave_nothing(tmp_path):
    arch = _arch(tmp_path, ["WW0WWV"])
    src = _scratch(arch, tmp_path, "K1ABC_1512.351.wav", "PJ4_K1ABC_1500.000.wav")
    assert arch.file_cycle([src], band="20", when=WHEN, mode="W2",
                           dial_hz=14095600) == 0
    assert _filed(tmp_path / "archive") == []
    assert not src.exists()


@pytest.mark.parametrize("calls", [[], ["*"], ["WW0WWV", "*"], None])
def test_wildcard_keeps_all(tmp_path, calls):
    arch = CarrierArchive(tmp_path / "archive", "AC0G-B1", calls=calls)
    src = _scratch(arch, tmp_path, "WW0WWV_1449.945.wav", "K1ABC_1512.351.wav")
    assert arch.file_cycle([src], band="30", when=WHEN, mode="W2",
                           dial_hz=10138700) == 2
    assert len(_filed(tmp_path / "archive")) == 2


@pytest.mark.parametrize("raw, base", [
    ("WW0WWV", "WW0WWV"), ("ww0wwv", "WW0WWV"), ("WW0WWV/P", "WW0WWV"),
    ("WW0WWV_P", "WW0WWV"), ("PJ4/K1ABC", "K1ABC"), ("PJ4_K1ABC", "K1ABC"),
    ("K1ABC/QRP", "K1ABC"), ("", ""),
])
def test_base_call(raw, base):
    assert base_call(raw) == base


def test_call_filter():
    assert call_filter(None) is None
    assert call_filter([]) is None
    assert call_filter(["*"]) is None
    assert call_filter(["ww0wwv/p", "K1ABC"]) == {"WW0WWV", "K1ABC"}


def test_config_parses_and_validates_calls(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(BASE)
    assert load_config(str(p)).carrier_archive.calls == ["WW0WWV"]
    p.write_text(BASE + '[carrier_archive]\nenabled = true\ncalls = ["*"]\n')
    assert load_config(str(p)).carrier_archive.calls == ["*"]
    p.write_text(BASE + '[carrier_archive]\ncalls = []\n')
    assert load_config(str(p)).carrier_archive.calls == []


@pytest.mark.parametrize("calls, ok", [
    (["WW0WWV"], True), (["WW0WWV/P", "k1abc"], True), ([], True), (["*"], True),
    ("WW0WWV", False), ([1], False), (["WW0 WWV"], False), (["../x"], False),
    ([""], False),
])
def test_calls_validation(calls, ok):
    errs = [e for e in CarrierArchiveConfig(calls=calls).validate()
            if "calls" in e]
    assert (errs == []) is ok, errs
