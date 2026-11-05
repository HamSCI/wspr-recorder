"""Startup waits out an absent radiod (AC0G-B2, 2026-10-15 / 2026-11-04).

The recorder used to try once, exit 0, and let systemd's start limit lock
the unit out.  It now reports READY, waits 5, 10, 20, 40, 60 s, and retries.
"""
import asyncio
import logging
import os
import signal
import threading
import time

from wspr_recorder.__main__ import WsprRecorder


class FakeRM:
    _status_address = "bee1-hf-status.local"

    def __init__(self, fail_times, log_each=False):
        self.log_each = log_each
        self.fail_times = fail_times
        self.calls = 0
        self.quiet_failures = False

    def connect(self):
        self.calls += 1
        if self.calls <= self.fail_times and self.log_each:
            logging.getLogger("ka9q.control").error(
                "Unexpected error connecting to radiod", exc_info=True)
        return self.calls > self.fail_times

    def shutdown(self):
        self.shutdowns = getattr(self, "shutdowns", 0) + 1


def make(fail_times, shutdown_after=None, log_each=False):
    rec = object.__new__(WsprRecorder)
    rec.receiver_managers = {"a": FakeRM(fail_times, log_each)}
    rec._shutdown_event = asyncio.Event()
    rec.sent = []
    rec.delays = []
    events = rec.sent
    rec._sd_notify = lambda m: events.append(m)

    async def fake_wait(seconds):
        rec.delays.append(seconds)
        events.append(("wait", seconds))
        if shutdown_after is not None and len(rec.delays) >= shutdown_after:
            return True
        return False

    rec._wait_interruptible = fake_wait
    return rec


def test_startup_waits_for_an_unresolvable_radiod_instead_of_exiting():
    rec = make(3)
    n = asyncio.run(rec._connect_with_backoff())
    assert n == 1
    assert rec.delays == [5, 10, 20]


def test_delays_cap_at_sixty():
    rec = make(8)
    asyncio.run(rec._connect_with_backoff())
    assert rec.delays == [5, 10, 20, 40, 60, 60, 60, 60]


def test_waiting_logs_once_per_episode(caplog):
    rec = make(4)
    with caplog.at_level(logging.INFO):
        asyncio.run(rec._connect_with_backoff())
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if "reachable after" in r.getMessage()]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(warns) == 1
    assert len(infos) == 1
    assert len(errors) <= 1  # the first attempt only


def test_notify_ready_precedes_the_wait():
    rec = make(2)
    asyncio.run(rec._connect_with_backoff())
    first_wait = next(i for i, e in enumerate(rec.sent) if isinstance(e, tuple))
    ready = next(i for i, e in enumerate(rec.sent)
                 if isinstance(e, bytes) and e.startswith(b"READY=1"))
    assert ready < first_wait
    assert b"STATUS=waiting for radiod bee1-hf-status.local" in rec.sent[ready]
    assert rec.sent[-1] == b"STATUS=running"


def test_shutdown_interrupts_the_wait():
    rec = make(99, shutdown_after=2)
    assert asyncio.run(rec._connect_with_backoff()) == 0
    assert rec.delays == [5, 10]


def test_ka9q_per_try_errors_are_filtered_during_the_episode(caplog):
    rec = make(4, log_each=True)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(rec._connect_with_backoff())
    first_warn = next(i for i, r in enumerate(caplog.records)
                      if r.levelno == logging.WARNING)
    after = [r for r in caplog.records[first_warn + 1:]
             if r.name.startswith("ka9q")]
    assert after == []
    assert logging.getLogger("ka9q.control").filters == []
    assert rec.receiver_managers["a"].shutdowns == 4  # half-built state dropped


def test_ka9q_filter_removed_on_shutdown():
    rec = make(99, shutdown_after=1, log_each=True)
    asyncio.run(rec._connect_with_backoff())
    assert logging.getLogger("ka9q.control").filters == []


def test_quiet_failures_reset_after_the_episode():
    rec = make(2)
    asyncio.run(rec._connect_with_backoff())
    assert rec.receiver_managers["a"].quiet_failures is False


def test_wait_keeps_petting_the_watchdog(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "200000")  # slices of 0.1 s
    rec = object.__new__(WsprRecorder)
    rec._shutdown_event = asyncio.Event()
    sent = []
    rec._sd_notify = sent.append
    assert asyncio.run(rec._wait_interruptible(0.45)) is False
    assert sent.count(b"WATCHDOG=1") >= 2


def test_sigterm_during_the_wait_ends_it_within_a_second():
    rec = object.__new__(WsprRecorder)
    rec._sd_notify = lambda m: None
    old = signal.getsignal(signal.SIGUSR1)
    signal.signal(signal.SIGUSR1, lambda *a: rec.request_shutdown())

    async def go():
        rec._shutdown_event = asyncio.Event()
        rec._loop = asyncio.get_running_loop()
        threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGUSR1)).start()
        t0 = time.monotonic()
        stopped = await rec._wait_interruptible(3)
        return stopped, time.monotonic() - t0

    try:
        stopped, took = asyncio.run(go())
    finally:
        signal.signal(signal.SIGUSR1, old)
    assert stopped is True
    assert took < 1.0
