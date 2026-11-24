"""The recorder hands its decoders a CarrierArchive only when enabled, names
the receiver after the reporter id, and purges on the cleanup timer."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from wspr_recorder.__main__ import WsprRecorder
from wspr_recorder.callsign_db import CallsignDB
from wspr_recorder.config import CarrierArchiveConfig, Config, SourceConfig


def _rec(tmp_path, ca: CarrierArchiveConfig, reporter_id="AC0G/S",
         sources=1) -> WsprRecorder:
    rec = object.__new__(WsprRecorder)
    cfg = Config()
    cfg.radiod.status_address = "bee1-status.local"
    cfg.carrier_archive = ca
    cfg.sources = [SourceConfig(key=f"radiod:rx{i}-status.local",
                                status_address=f"rx{i}-status.local")
                   for i in range(sources)]
    rec.config = cfg
    rec._reporter_id = reporter_id
    rec.callsign_db = CallsignDB()
    rec.wav_writer = SimpleNamespace(output_dir=tmp_path / "shm")
    rec._decoders = {}
    return rec


def test_disabled_gives_decoders_no_archive(tmp_path):
    rec = _rec(tmp_path, CarrierArchiveConfig())
    runner = rec._resolve_decoder("20", 14_095_600, rx_source="radiod:rx0-status.local")
    assert runner.carrier_archive is None


def test_enabled_gives_decoders_an_archive_named_for_the_reporter(tmp_path):
    rec = _rec(tmp_path, CarrierArchiveConfig(enabled=True,
                                              root=str(tmp_path / "ca")))
    runner = rec._resolve_decoder("20", 14_095_600, rx_source="radiod:rx0-status.local")
    a = runner.carrier_archive
    assert a.root == tmp_path / "ca"
    assert a.receiver == "AC0G-S"                       # '/' made safe


def test_receiver_falls_back_to_radiod_and_adds_source_when_several(tmp_path):
    rec = _rec(tmp_path, CarrierArchiveConfig(enabled=True), reporter_id=None)
    assert rec._carrier_receiver("radiod:rx0-status.local") == "bee1"
    rec = _rec(tmp_path, CarrierArchiveConfig(enabled=True), sources=2)
    assert (rec._carrier_receiver("radiod:rx1-status.local")
            == "AC0G/S_rx1-status.local")


def test_purge_runs_only_when_enabled(tmp_path):
    old = tmp_path / "ca" / "20200101"
    old.mkdir(parents=True)
    _rec(tmp_path, CarrierArchiveConfig(root=str(tmp_path / "ca"))
         )._purge_carrier_archive()
    assert old.exists()
    _rec(tmp_path, CarrierArchiveConfig(enabled=True, root=str(tmp_path / "ca"))
         )._purge_carrier_archive()
    assert not old.exists()
