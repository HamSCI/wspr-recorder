"""Carrier-archive purge: by age, then by disk use (oldest day first).

Mirrors carrier-archive.sh carrier_archive_purge: keep max_age_days whole UTC
days, today included; then, while the file system is fuller than
max_disk_pct, delete the oldest day, but never the newest.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from wspr_recorder.carrier_archive import (
    MARKER, CarrierArchive, disk_used_pct, purge,
)

NOW = datetime(2026, 11, 23, 14, 8, tzinfo=timezone.utc)


def _days(root: Path, *names: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).touch()                  # an archive root (see fix1 M-4)
    for n in names:
        d = root / n / "AC0G-B1" / "20"
        d.mkdir(parents=True)
        (d / "1408_W2_K1ABC_14097100.124.wav").write_bytes(b"x")


def _left(root: Path):
    return sorted(p.name for p in root.iterdir() if p.name != MARKER)


def _never_full(path):
    return 10.0


def test_age_purge_keeps_today_and_previous_days(tmp_path):
    _days(tmp_path, "20261119", "20261120", "20261121", "20261122", "20261123")
    removed = purge(tmp_path, max_age_days=3, max_disk_pct=90, now=NOW,
                    disk_used_pct=_never_full)
    assert _left(tmp_path) == ["20261121", "20261122", "20261123"]
    assert sorted(Path(p).name for p in removed) == ["20261119", "20261120"]


def test_age_purge_leaves_non_day_entries_alone(tmp_path):
    _days(tmp_path, "20200101")
    (tmp_path / "notes").mkdir()
    (tmp_path / "2020010").mkdir()               # not 8 digits
    (tmp_path / "20200102").write_text("a file, not a day dir")
    purge(tmp_path, max_age_days=3, max_disk_pct=90, now=NOW,
          disk_used_pct=_never_full)
    assert _left(tmp_path) == ["2020010", "20200102", "notes"]


def test_age_purge_does_not_follow_symlinks(tmp_path):
    outside = tmp_path / "outside"
    _days(outside, "keepme")
    root = tmp_path / "root"
    root.mkdir()
    (root / MARKER).touch()
    (root / "20200101").symlink_to(outside / "keepme", target_is_directory=True)
    purge(root, max_age_days=3, max_disk_pct=90, now=NOW,
          disk_used_pct=_never_full)
    assert (outside / "keepme" / "AC0G-B1" / "20").is_dir()


def test_disk_purge_deletes_oldest_until_under(tmp_path):
    _days(tmp_path, "20261121", "20261122", "20261123")
    levels = iter([97.0, 93.0, 85.0])           # one day freed per check
    calls = []

    def fake_pct(path):
        calls.append(path)
        return next(levels)

    purge(tmp_path, max_age_days=30, max_disk_pct=90, now=NOW,
          disk_used_pct=fake_pct)
    assert _left(tmp_path) == ["20261123"]
    assert len(calls) == 3


def test_disk_purge_never_deletes_the_newest_day(tmp_path):
    _days(tmp_path, "20261122", "20261123")
    purge(tmp_path, max_age_days=30, max_disk_pct=90, now=NOW,
          disk_used_pct=lambda p: 99.0)
    assert _left(tmp_path) == ["20261123"]


def test_disk_purge_stops_when_usage_unknown(tmp_path):
    _days(tmp_path, "20261122", "20261123")
    purge(tmp_path, max_age_days=30, max_disk_pct=90, now=NOW,
          disk_used_pct=lambda p: None)
    assert _left(tmp_path) == ["20261122", "20261123"]


def test_missing_root_is_a_no_op(tmp_path):
    assert purge(tmp_path / "nope", max_age_days=3, max_disk_pct=90,
                 now=NOW, disk_used_pct=_never_full) == []


def test_purge_never_raises(tmp_path, monkeypatch):
    _days(tmp_path, "20200101")

    def boom(path):
        raise OSError("gone")

    import wspr_recorder.carrier_archive as ca
    monkeypatch.setattr(ca.shutil, "rmtree", boom)
    purge(tmp_path, max_age_days=3, max_disk_pct=90, now=NOW,
          disk_used_pct=_never_full)               # logged, not raised


def test_archive_purge_uses_its_settings(tmp_path):
    root = tmp_path / "archive"
    _days(root, "20261101", "20261123")
    a = CarrierArchive(root=root, receiver="R", max_age_days=3,
                       max_disk_pct=90, disk_used_pct=_never_full)
    a.purge(now=NOW)
    assert _left(root) == ["20261123"]


def test_real_disk_used_pct(tmp_path):
    pct = disk_used_pct(tmp_path)
    assert pct is not None and 0.0 <= pct <= 100.0
    assert disk_used_pct(tmp_path / "missing") is None
