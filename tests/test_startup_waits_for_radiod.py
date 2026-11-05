"""Startup waits out an absent radiod (AC0G-B2, 2026-10-15 / 2026-11-04).

The recorder used to try once, exit 0, and let systemd's start limit lock
the unit out.  It now reports READY, waits 5, 10, 20, 40, 60 s, and retries.
"""
import asyncio
import logging

from wspr_recorder.__main__ import WsprRecorder


class FakeRM:
    _status_address = "bee1-hf-status.local"

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0
        self.quiet_failures = False

    def connect(self):
        self.calls += 1
        return self.calls > self.fail_times


def make(fail_times, shutdown_after=None):
    rec = object.__new__(WsprRecorder)
    rec.receiver_managers = {"a": FakeRM(fail_times)}
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
