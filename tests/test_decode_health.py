"""Decode-health ledger: a slow decode is fine, a band that stops recovering is not."""
import time

import pytest

from wspr_recorder.decode_health import DecodeHealth, read_event_log
from wspr_recorder.wspr_ctl import render_decode_health, summarise_events


@pytest.fixture
def health(tmp_path):
    return DecodeHealth(late_s=120, sustain_cycles=5,
                        event_log=tmp_path / "decode-health.jsonl", enabled=True)


def feed(health, band, latenesses, elapsed=20.0, period_s=120):
    """Decode one slot per lateness, as one band's decoder would."""
    out = []
    now = time.time()
    for i, late in enumerate(latenesses):
        cycle = now - (len(latenesses) - i) * period_s
        out.append(health.record(band=band, mode="W2", period_s=period_s,
                                 cycle_start=cycle,
                                 decode_start=cycle + period_s + late,
                                 elapsed_s=elapsed))
    return out


# ── the cases that must stay quiet ──────────────────────────────────────

def test_a_prompt_decode_is_ok(health):
    assert feed(health, "20", [2])[0].status == "OK"
    assert health.summary()["cycles_missed"] == 0


def test_a_decode_longer_than_its_cycle_is_not_a_fault(health):
    """The :30 F15+F30 wave: 131 s of decode, still on time for the next slot."""
    e = feed(health, "20", [4], elapsed=131.0)[0]
    assert e.status == "OK"
    assert health.bands_behind() == {}


def test_the_thirty_minute_wave_drains_and_never_reports(health):
    """An F30 decode blocks the band ~5 min, then it catches up by itself."""
    events = feed(health, "40", [300, 190, 80, 10, 4, 5])
    assert [e.status for e in events] == ["LATE", "LATE", "CAUGHT_UP", "OK", "OK", "OK"]
    assert health.bands_behind() == {}
    assert health.issues() == []


def test_a_long_but_draining_run_never_reports(health):
    """Nine cycles, seven of them over the threshold — but falling the whole way."""
    events = feed(health, "40", [900, 800, 700, 600, 500, 400, 300, 200, 100])
    assert not any(e.status == "BEHIND" for e in events)
    assert events[-1].status == "CAUGHT_UP"
    assert health.issues() == []


# ── the cases that must speak up ────────────────────────────────────────

def test_a_sustained_slide_reports_once_then_reminds(health):
    events = feed(health, "40", [130, 260, 390, 520, 650, 780, 910, 1040, 1170, 1300, 1430])
    behind = [i for i, e in enumerate(events) if e.status == "BEHIND"]
    assert behind == [4, 9]              # at the 5th late cycle, then every 5th after
    assert "no progress" in events[4].detail
    assert health.bands_behind()["40"]["late_cycles"] == 11
    assert any("not recovering" in i for i in health.issues())


def test_a_band_stuck_at_a_constant_lag_reports(health):
    """Running exactly at capacity: never worse, never better, never recovers."""
    events = feed(health, "40", [600] * 6)
    assert events[4].status == "BEHIND"


def test_recovery_after_a_reported_episode_clears_it(health):
    feed(health, "40", [130, 260, 390, 520, 650])
    assert health.bands_behind()
    e = feed(health, "40", [5])[0]
    assert e.status == "CAUGHT_UP"
    assert health.bands_behind() == {}
    assert health.issues() == []


def test_a_killed_decode_is_a_lost_cycle(health):
    now = time.time()
    e = health.record(band="40", mode="W2", period_s=120, cycle_start=now - 230,
                      decode_start=now - 110, elapsed_s=110, killed=True)
    assert e.status == "KILLED"
    assert health.summary()["cycles_missed"] == 1
    assert any("killed" in i for i in health.issues())


def test_a_cycle_whose_audio_is_gone_is_dropped(health):
    e = health.record_drop(band="15", period_s=1800, cycle_start=time.time() - 2400,
                           detail="audio no longer resident in the ring")
    assert e.status == "DROPPED"
    assert health.summary()["cycles_missed"] == 1


# ── bookkeeping ─────────────────────────────────────────────────────────

def test_each_band_keeps_its_own_streak(health):
    feed(health, "40", [130, 260, 390, 520, 650])
    feed(health, "20", [5, 5, 5])
    assert set(health.bands_behind()) == {"40"}


def test_lateness_never_goes_negative(health):
    now = time.time()
    e = health.record(band="20", mode="W2", period_s=120, cycle_start=now,
                      decode_start=now, elapsed_s=1)
    assert e.late_s == 0.0


def test_events_persist_so_the_history_outlives_a_restart(health, tmp_path):
    feed(health, "40", [130, 260, 390, 520, 650])
    events = read_event_log(tmp_path / "decode-health.jsonl")
    assert [e["status"] for e in events][-1] == "BEHIND"


def test_an_unwritable_log_does_not_break_decoding(tmp_path):
    (tmp_path / "nope").mkdir()
    h = DecodeHealth(event_log=tmp_path / "nope")     # a directory where a file must go
    assert feed(h, "20", [2])[0].status == "OK"       # judged in memory regardless
    assert h.summary()["decodes_total"] == 1


def test_disabled_ledger_records_nothing(tmp_path):
    h = DecodeHealth(event_log=tmp_path / "x.jsonl", enabled=False)
    feed(h, "20", [400])
    assert h.summary()["counts"] == {}


def test_healthy_decodes_do_not_flood_the_ledger(health, tmp_path):
    """14 bands x 30 cycles/hour would push every episode out of the ring."""
    feed(health, "20", [3] * 30)
    stored = read_event_log(tmp_path / "decode-health.jsonl")
    assert len(stored) == 1                       # one heartbeat, not thirty
    assert health.summary()["decodes_total"] == 30
    assert health.summary()["last_per_band"]["20"]["status"] == "OK"


def test_the_ctl_report_reads_the_event_log_when_the_daemon_is_down(health, tmp_path):
    feed(health, "40", [130, 260, 390, 520, 650])
    now = time.time()
    health.record(band="30", mode="W2", period_s=120, cycle_start=now - 230,
                  decode_start=now - 110, elapsed_s=110, killed=True)
    summary = summarise_events(read_event_log(tmp_path / "decode-health.jsonl"), 24.0)
    assert summary["cycles_missed"] == 1
    assert "40" in summary["bands_behind"]
    report = render_decode_health(summary)
    assert "NOT RECOVERING" in report
    assert "KILLED 1" in report
