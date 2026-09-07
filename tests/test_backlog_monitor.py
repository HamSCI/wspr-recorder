"""Tests for the decode-backlog monitor (stuck / growing rules) and the pool
snapshot it reads.  Ported in spirit from wsprdaemon's wd-decode-backlog.sh
after K6FOD's 42 GB FT8 pile-up went unnoticed."""

import logging
import threading
import time
from dataclasses import dataclass

from wspr_recorder.backlog_monitor import BacklogMonitor
from wspr_recorder.decode_pool import PriorityDecodePool


@dataclass
class _Req:
    period_seconds: int


def _snap(queued, oldest=0.0, running=0, workers=4):
    return {"queued": queued, "oldest_wait_s": oldest, "running": running, "workers": workers}


def test_quiet_pool_is_ok():
    m = BacklogMonitor(workers=4, warn_wait_s=120, floor=4, grow_samples=5)
    a = m.assess(_snap(0))
    assert a.level == "ok"


def test_spike_at_top_of_hour_is_not_a_warning():
    """All cadences fire at :00 — a deep but young queue is normal."""
    m = BacklogMonitor(workers=4, warn_wait_s=120, floor=4, grow_samples=5)
    assert m.assess(_snap(30, oldest=20)).level == "ok"


def test_stuck_when_oldest_job_waited_a_full_period():
    m = BacklogMonitor(workers=4, warn_wait_s=120, floor=4, grow_samples=5)
    a = m.assess(_snap(3, oldest=130))
    assert a.level == "warn" and "STUCK" in a.reason


def test_growing_over_five_samples_without_shrinking():
    m = BacklogMonitor(workers=4, warn_wait_s=120, floor=4, grow_samples=5)
    levels = [m.assess(_snap(q, oldest=10)).level for q in (4, 5, 6, 7, 8, 9)]
    assert levels[:-1] == ["ok"] * 5 and levels[-1] == "warn"


def test_a_dip_resets_the_growth_rule():
    m = BacklogMonitor(workers=4, warn_wait_s=120, floor=4, grow_samples=5)
    for q in (4, 5, 6, 4, 8, 9):
        a = m.assess(_snap(q, oldest=10))
    assert a.level == "ok"


def test_below_floor_never_warns_on_growth():
    m = BacklogMonitor(workers=4, warn_wait_s=120, floor=4, grow_samples=3)
    for q in (0, 1, 2, 3):
        a = m.assess(_snap(q))
    assert a.level == "ok"


def test_observe_logs_entry_reminder_and_recovery(caplog):
    m = BacklogMonitor(workers=4, warn_wait_s=120, floor=4, grow_samples=5, remind_s=600,
                       log=logging.getLogger("t.backlog"))
    with caplog.at_level(logging.INFO, logger="t.backlog"):
        m.observe(_snap(2, oldest=200), now=1000)      # entry
        m.observe(_snap(2, oldest=260), now=1060)      # still warn, no reminder yet
        m.observe(_snap(2, oldest=900), now=1700)      # reminder after 600 s
        m.observe(_snap(0), now=1760)                  # recovery
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("STUCK" in x for x in msgs) == 2
    assert any("persisting" in x for x in msgs)
    assert any("cleared" in x for x in msgs)
    assert m.as_dict()["level"] == "ok"


def test_pool_snapshot_reports_queue_age_and_running():
    gate = threading.Event()
    pool = PriorityDecodePool(1)
    try:
        pool.submit(lambda r: gate.wait(5), _Req(120))   # occupies the only worker
        time.sleep(0.05)
        pool.submit(lambda r: None, _Req(120))           # queued behind it
        time.sleep(0.2)
        snap = pool.backlog_snapshot()
        assert snap["running"] == 1 and snap["queued"] == 1 and snap["workers"] == 1
        assert snap["oldest_wait_s"] >= 0.15
        gate.set()
        time.sleep(0.3)
        snap = pool.backlog_snapshot()
        assert snap["queued"] == 0 and snap["running"] == 0 and snap["oldest_wait_s"] == 0.0
    finally:
        gate.set()
        pool.shutdown()
