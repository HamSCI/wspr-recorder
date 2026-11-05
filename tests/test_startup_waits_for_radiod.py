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


class BlockingRM(FakeRM):
    def __init__(self, block_s):
        super().__init__(0)
        self.block_s = block_s

    def connect(self):
        self.calls += 1
        if self.calls == 2:
            time.sleep(self.block_s)   # blocks a worker thread, as a silent radiod does
        return self.calls >= 3


def test_watchdog_is_petted_while_connect_blocks(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "2000000")  # period floor 1 s
    rec = make(0)
    rec.receiver_managers = {"a": BlockingRM(2.5)}
    asyncio.run(rec._connect_with_backoff())
    assert rec.sent.count(b"WATCHDOG=1") >= 2
    assert not [t for t in threading.enumerate() if t.name == "watchdog-pet"]


def test_no_watchdog_thread_without_watchdog_usec(monkeypatch):
    for bad in (None, "0", "-5", "junk"):
        if bad is None:
            monkeypatch.delenv("WATCHDOG_USEC", raising=False)
        else:
            monkeypatch.setenv("WATCHDOG_USEC", bad)
        rec = make(2)
        asyncio.run(rec._connect_with_backoff())
        assert b"WATCHDOG=1" not in rec.sent


def test_watchdog_thread_stopped_after_shutdown(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "2000000")
    rec = make(99, shutdown_after=1)
    asyncio.run(rec._connect_with_backoff())
    assert not [t for t in threading.enumerate() if t.name == "watchdog-pet"]


def test_client_receiver_manager_logs_are_quiet_during_the_episode(caplog):
    rml = logging.getLogger("wspr_recorder.receiver_manager")

    class Noisy(FakeRM):
        def connect(self):
            self.calls += 1
            rml.info("Connecting to radiod at x")
            rml.error("Failed to provision 20m")
            rml.info("Provisioned 0/17 channels")
            return self.calls > self.fail_times

    rec = make(0)
    rec.receiver_managers = {"a": Noisy(3)}
    with caplog.at_level(logging.DEBUG):
        asyncio.run(rec._connect_with_backoff())
        rml.info("after success")
    recs = caplog.records
    w = next(i for i, r in enumerate(recs) if r.levelno == logging.WARNING)
    between = [r for r in recs[w + 1:]
               if r.name == "wspr_recorder.receiver_manager"
               and r.getMessage() != "after success"]
    assert between == []
    assert recs[-1].getMessage() == "after success"


def test_shutdown_clears_stopped_streams():
    from wspr_recorder.receiver_manager import ReceiverManager
    rm = object.__new__(ReceiverManager)
    rm._status_listener = None
    rm._control = None
    rm.stop_streams = lambda: None
    class St: connected = True; channels = {1: object()}
    rm.state = St()
    rm._multi_by_group = {("g", 1): object()}
    rm._lifetime_entries = [1]
    rm.shutdown()
    assert rm.state.channels == {} and rm._multi_by_group == {}
    assert rm._lifetime_entries == []


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
