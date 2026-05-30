"""Tests for the priority decode pool (short periods preempt long)."""

import threading
import time
from dataclasses import dataclass

from wspr_recorder.decode_pool import PriorityDecodePool, LONG_PERIOD_SECONDS


@dataclass
class _Req:
    """Minimal stand-in for DecodeRequest — only period_seconds matters."""
    period_seconds: int


def test_submit_runs_and_returns_result():
    pool = PriorityDecodePool(2)
    try:
        fut = pool.submit(lambda r: r.period_seconds * 2, _Req(120))
        assert fut.result(timeout=5) == 240
    finally:
        pool.shutdown()


def test_exceptions_land_on_future():
    pool = PriorityDecodePool(1)
    try:
        def boom(_):
            raise ValueError("nope")
        fut = pool.submit(boom, _Req(120))
        try:
            fut.result(timeout=5)
            assert False, "expected exception"
        except ValueError as exc:
            assert str(exc) == "nope"
    finally:
        pool.shutdown()


def test_short_periods_run_before_long():
    """With a single worker and a full queue, execution order must be
    strictly by ascending period regardless of submit order."""
    order = []
    gate = threading.Event()

    def record(r):
        gate.wait(5)        # hold the worker until every job is queued
        order.append(r.period_seconds)

    pool = PriorityDecodePool(1)
    try:
        # Submit long-first, deliberately out of priority order.
        for p in (1800, 900, 300, 120, 120):
            pool.submit(record, _Req(p))
        # Let the worker drain the now-fully-populated queue.
        gate.set()
        pool.shutdown(wait=True)
    finally:
        pass
    # First job may have been picked up before the gate; the REMAINING
    # queued jobs must come out shortest-first.  The two 120s jobs sort
    # ahead of 300, 900, 1800.
    assert order[-3:] == [300, 900, 1800], order
    assert order.count(120) == 2


def test_long_reservation_keeps_a_worker_for_short():
    """A short job submitted while long jobs saturate the long-slot cap
    must start immediately rather than queue behind them."""
    started = []
    release = threading.Event()

    def long_job(r):
        started.append(("long", r.period_seconds))
        release.wait(5)     # occupy the long slot

    def short_job(r):
        started.append(("short", r.period_seconds))

    # 3 workers, but only 1 may run a long decode at a time.
    pool = PriorityDecodePool(3, max_long_inflight=1)
    try:
        # Two long jobs: one runs, one is capped/queued.
        pool.submit(long_job, _Req(1800))
        pool.submit(long_job, _Req(1800))
        # Give the first long job time to occupy its slot.
        time.sleep(0.2)
        # A short job must NOT wait for the long jobs to finish.
        fut = pool.submit(short_job, _Req(120))
        fut.result(timeout=2)            # would time out if starved
        assert ("short", 120) in started
        # Only one long job should have started (cap = 1).
        assert started.count(("long", 1800)) == 1
        release.set()
        pool.shutdown(wait=True)
    finally:
        release.set()


def test_shutdown_drains_capped_long_jobs():
    """cancel_futures=False must still run queued long jobs at shutdown
    (so wait=True doesn't strand them) even when over the long cap."""
    done = []
    pool = PriorityDecodePool(1, max_long_inflight=1)
    for p in (1800, 1800, 900):
        pool.submit(lambda r: done.append(r.period_seconds), _Req(p))
    pool.shutdown(wait=True)
    assert sorted(done) == [900, 1800, 1800]


def test_pending_counts_queued_jobs():
    block = threading.Event()
    pool = PriorityDecodePool(1)
    try:
        pool.submit(lambda r: block.wait(5), _Req(120))  # occupies worker
        time.sleep(0.1)
        pool.submit(lambda r: None, _Req(120))
        pool.submit(lambda r: None, _Req(1800))
        assert pool.pending() == 2
        block.set()
        pool.shutdown(wait=True)
    finally:
        block.set()


def test_no_period_attr_sorts_as_short():
    """Callers without period_seconds (non-decode) run ASAP (priority 0)."""
    pool = PriorityDecodePool(1)
    try:
        fut = pool.submit(lambda: 7)     # no args, no period_seconds
        assert fut.result(timeout=5) == 7
    finally:
        pool.shutdown()


def test_threshold_classification():
    """F5 (300s) is short; F15 (900s) is the long boundary."""
    assert 300 < LONG_PERIOD_SECONDS <= 900
