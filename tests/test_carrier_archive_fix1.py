"""Carrier archive review fixes, round 1 (I-1, M-1 .. M-4)."""
from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import wspr_recorder.carrier_archive as ca_mod
from wspr_recorder.carrier_archive import MARKER, CarrierArchive, purge
from wspr_recorder.config import CarrierArchiveConfig, Config, load_config
from wspr_recorder.wav_writer import WavWriter

NOW = datetime(2026, 11, 23, 14, 8, tzinfo=timezone.utc)


def _wav(d: Path, name: str, body: bytes = b"RIFF") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(body)
    return p


def _root(tmp_path: Path, *days: str) -> Path:
    root = tmp_path / "archive"
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).touch()
    for d in days:
        _wav(root / d / "R" / "20", "1408_W2_K1ABC_14097100.124.wav")
    return root


def _left(root: Path):
    return sorted(p.name for p in root.iterdir() if p.name != MARKER)


# ---- I-1: the recorder's WAV cleanup never sees carrier scratch ------------

def test_scratch_leaf_is_hidden(tmp_path):
    a = CarrierArchive(tmp_path / "archive", "R")
    d = a.scratch_dir(tmp_path / "20" / ".phase2", "p1")
    assert d.name.startswith(".")
    assert d.parent.name == "carrier.d"


def test_enforce_max_files_skips_scratch(tmp_path, caplog):
    """The reviewer's repro: 40 carrier wavs mid-decode in a scratch dir."""
    out = tmp_path / "shm"
    a = CarrierArchive(tmp_path / "archive", "R")
    scratch = a.scratch_dir(out / "20" / ".phase2", "p1")
    for i in range(40):
        _wav(scratch, f"K{i}ABC_1500.{i:03d}.wav")
    w = WavWriter(output_dir=out)
    with caplog.at_level(logging.WARNING):
        assert w.enforce_max_files_per_band(35) == 0
    assert len(list(scratch.glob("*.wav"))) == 40
    assert "exceeds limit" not in caplog.text


def test_cleanup_old_files_skips_scratch(tmp_path):
    out = tmp_path / "shm"
    a = CarrierArchive(tmp_path / "archive", "R")
    scratch = a.scratch_dir(out / "20" / ".phase2", "p1")
    old = _wav(scratch, "K1ABC_1500.000.wav")
    os.utime(old, (1, 1))
    # an orphan from the pre-fix, un-dotted layout is skipped too
    legacy = _wav(out / "20" / ".phase2" / "carrier.d" / "p1.abcd",
                  "K2ABC_1500.000.wav")
    os.utime(legacy, (1, 1))
    band_wav = _wav(out / "20", "20261123T140800Z_14095600_usb_120.wav")
    os.utime(band_wav, (1, 1))
    assert WavWriter(output_dir=out).cleanup_old_files(35) == 1
    assert old.exists() and legacy.exists() and not band_wav.exists()


def test_stale_scratch_swept(tmp_path):
    """Scratch the cleanup cannot see is swept by the archive instead
    (leftovers of a crashed recorder), but never a live one."""
    a = CarrierArchive(tmp_path / "archive", "R")
    work = tmp_path / "work"
    stale = a.scratch_dir(work, "p1")
    _wav(stale, "K1ABC_1500.000.wav")
    os.utime(stale, (1, 1))
    legacy = work / "carrier.d" / "p1.abcd"
    legacy.mkdir()
    os.utime(legacy, (1, 1))
    live = a.scratch_dir(work, "p1")
    assert CarrierArchive.sweep_scratch(work, older_than_s=3600) == 2
    assert not stale.exists() and not legacy.exists() and live.exists()


# ---- M-1: no thread when disabled ------------------------------------------

def _rec(tmp_path, cfg: CarrierArchiveConfig):
    from wspr_recorder.__main__ import WsprRecorder
    rec = object.__new__(WsprRecorder)
    c = Config()
    c.carrier_archive = cfg
    rec.config = c
    rec.wav_writer = None
    return rec


def test_cleanup_tick_uses_no_thread_when_disabled(tmp_path, monkeypatch):
    calls = []

    async def fake_to_thread(fn, *a, **kw):
        calls.append(fn)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    asyncio.run(_rec(tmp_path, CarrierArchiveConfig())._cleanup_once())
    assert calls == []
    asyncio.run(_rec(tmp_path, CarrierArchiveConfig(
        enabled=True, root=str(tmp_path / "a")))._cleanup_once())
    assert len(calls) == 1


# ---- M-2: disk safety -------------------------------------------------------

def test_filing_skipped_over_hard_limit_and_logged_once(tmp_path, caplog):
    pct = {"v": 96.0}                      # max 90 -> hard limit 95
    a = CarrierArchive(_root(tmp_path), "R", max_disk_pct=90,
                       disk_used_pct=lambda p: pct["v"])
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            src = tmp_path / "p1"
            _wav(src, "K1ABC_1500.124.wav")
            assert a.file_cycle([src], band="20", when=NOW, mode="W2",
                                dial_hz=14_095_600) == 0
            assert not src.exists()
    assert caplog.text.count("full") == 1
    pct["v"] = 50.0
    src = tmp_path / "p1"
    _wav(src, "K1ABC_1500.124.wav")
    assert a.file_cycle([src], band="20", when=NOW, mode="W2",
                        dial_hz=14_095_600) == 1


def test_hard_limit_capped_at_98():
    assert CarrierArchive("/x", "R", max_disk_pct=90).hard_limit_pct == 95
    assert CarrierArchive("/x", "R", max_disk_pct=96).hard_limit_pct == 98
    assert CarrierArchive("/x", "R", max_disk_pct=100).hard_limit_pct == 98


def test_age_purge_removes_future_days(tmp_path):
    root = _root(tmp_path, "20261122", "20261123", "20261124", "20300101")
    purge(root, max_age_days=3, max_disk_pct=90, now=NOW,
          disk_used_pct=lambda p: 10.0)
    assert _left(root) == ["20261122", "20261123"]


def test_disk_purge_never_deletes_today(tmp_path):
    root = _root(tmp_path, "20261121", "20261122", "20261123")
    purge(root, max_age_days=30, max_disk_pct=90, now=NOW,
          disk_used_pct=lambda p: 99.0)
    assert _left(root) == ["20261123"]
    # only today left, still full: it stays (filing stops at the hard limit)
    purge(root, max_age_days=30, max_disk_pct=90, now=NOW,
          disk_used_pct=lambda p: 99.0)
    assert _left(root) == ["20261123"]


# ---- M-3: one warning per cycle; no partial targets ------------------------

def test_systematic_failure_logs_once_per_cycle(tmp_path, monkeypatch, caplog):
    a = CarrierArchive(_root(tmp_path), "R", disk_used_pct=lambda p: 10.0)
    src = tmp_path / "p1"
    for i in range(5):
        _wav(src, f"K{i}ABC_1500.000.wav")

    def erofs(s, d):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(ca_mod.shutil, "move", erofs)
    with caplog.at_level(logging.WARNING):
        assert a.file_cycle([src], band="20", when=NOW, mode="W2",
                            dial_hz=14_095_600) == 0
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) == 1
    assert "5" in warns[0].getMessage()
    assert "Read-only" in warns[0].getMessage()


def test_failed_cross_fs_move_leaves_no_partial_target(tmp_path, monkeypatch):
    root = _root(tmp_path)
    a = CarrierArchive(root, "R", disk_used_pct=lambda p: 10.0)
    src = tmp_path / "p1"
    _wav(src, "K1ABC_1500.124.wav")

    def half_move(s, d):
        Path(d).write_bytes(b"RIF")              # copy started ...
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(ca_mod.shutil, "move", half_move)
    assert a.file_cycle([src], band="20", when=NOW, mode="W2",
                        dial_hz=14_095_600) == 0
    assert list(root.rglob("*.wav")) == []


# ---- M-4: marker, root validation, browser --------------------------------

def test_purge_refuses_root_without_marker(tmp_path, caplog):
    root = tmp_path / "notours"
    (root / "20200101").mkdir(parents=True)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert purge(root, max_age_days=3, max_disk_pct=90, now=NOW,
                         disk_used_pct=lambda p: 10.0) == []
    assert (root / "20200101").exists()
    assert caplog.text.count(MARKER) == 1


def test_filing_creates_root_and_marker(tmp_path):
    root = tmp_path / "new" / "archive"
    a = CarrierArchive(root, "R", disk_used_pct=lambda p: 10.0)
    src = tmp_path / "p1"
    _wav(src, "K1ABC_1500.124.wav")
    assert a.file_cycle([src], band="20", when=NOW, mode="W2",
                        dial_hz=14_095_600) == 1
    assert (root / MARKER).is_file()


def test_ensure_root_stamps_new_empty_or_archive_like_roots_only(tmp_path):
    assert CarrierArchive(tmp_path / "fresh", "R").ensure_root() is True
    assert (tmp_path / "fresh" / MARKER).exists()
    looks = tmp_path / "looks"
    (looks / "20261122").mkdir(parents=True)
    assert CarrierArchive(looks, "R").ensure_root() is True
    foreign = tmp_path / "foreign"
    (foreign / "etc").mkdir(parents=True)
    assert CarrierArchive(foreign, "R").ensure_root() is False
    assert not (foreign / MARKER).exists()


BASE = """
[recorder]
output_dir = "/dev/shm/wspr-recorder"

[radiod]
status = "test-status.local"

[[band]]
frequency = "14095600"
modes = ["W2"]
"""


@pytest.mark.parametrize("root", [
    "/", "//", "/dev", "/dev/shm", "/dev/shm/wspr-recorder", "/var",
    "/var/lib", "/var/lib/sigmond", "/var/lib/../lib",
])
def test_config_rejects_dangerous_roots(tmp_path, root):
    p = tmp_path / "c.toml"
    p.write_text(BASE + f'\n[carrier_archive]\nroot = "{root}"\n')
    with pytest.raises(ValueError, match="carrier_archive.root"):
        load_config(str(p))


def test_config_accepts_default_and_sibling_roots(tmp_path):
    for root in ("/var/lib/wspr-recorder/carrier-archive",
                 "/var/lib/sigmond-carriers", "/srv/carriers"):
        p = tmp_path / "c.toml"
        p.write_text(BASE + f'\n[carrier_archive]\nroot = "{root}"\n')
        assert load_config(str(p)).carrier_archive.root == root


def test_browser_refuses_root_slash(capsys, monkeypatch):
    from wspr_recorder import carrier_browser as cb

    def served(self):
        raise AssertionError("served / instead of refusing it")

    monkeypatch.setattr(cb.ThreadingHTTPServer, "serve_forever", served)
    with pytest.raises(SystemExit) as e:
        cb.main(["--root", "/", "--port", "0"])
    assert e.value.code != 0
    with pytest.raises(ValueError):
        cb.build_server("/", "127.0.0.1", 0)
