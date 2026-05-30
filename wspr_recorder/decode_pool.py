"""Priority decode pool — short WSPR periods preempt long ones.

Every WSPR cycle, many decode jobs land on the executor at once: at the
top of the hour a single minute boundary completes W2 + F2 (120 s),
F5 (300 s), F15 (900 s) AND F30 (1800 s) across every enabled band.
A plain ``ThreadPoolExecutor`` runs them first-come-first-served, so a
handful of memory-heavy, slow F30 decodes can occupy every worker
thread while the time-critical 2-minute W2 decodes wait behind them —
and a W2 that doesn't finish inside its ~110 s window is effectively
lost (its spots spill into the wrong cycle, which is the "uploads at
weird times / chaos" an operator sees).

This pool fixes the ordering the way the operator asked for it:

  * **Short periods always run first.**  An idle worker takes the
    shortest-period job available; a long (>= ``LONG_PERIOD_SECONDS``)
    job is only ever started when there is NO shorter job waiting.  So
    every 2-minute decode in a cycle drains before any F15/F30 begins.

  * **Long decodes can't monopolise the pool.**  At most
    ``max_long_inflight`` long jobs run at once; the remaining workers
    are reserved so a short decode arriving mid-F30-wave starts
    immediately instead of queueing behind the long jobs.  This is the
    per-process complement to the host-wide F15/F30 memory semaphore in
    ``host_slot.py`` (which bounds long-decode memory ACROSS instances);
    here we bound how many of THIS process's workers a long decode may
    hold so short work is never starved.

Only the slice of ``concurrent.futures.ThreadPoolExecutor`` that
wspr-recorder actually uses is implemented: ``submit(fn, request)``
returning a ``Future``, ``shutdown(wait=, cancel_futures=)``, the
``_max_workers`` attribute, and a ``pending()`` count for status.  The
job's priority is read from the first positional arg's
``period_seconds`` attribute (the ``DecodeRequest``); anything without
one sorts as highest priority (runs ASAP), so non-decode callers are
unaffected.
"""

from __future__ import annotations

import heapq
import logging
import os
import threading
from concurrent.futures import Future
from typing import Any, Callable, List, Optional, Tuple


logger = logging.getLogger(__name__)


# A decode whose period is >= this many seconds is "long": memory-heavy
# (43 MB F15 / 86 MB F30 float32 slices) and slow.  F5 (300 s) and the
# 2-minute modes stay "short".  Matches the host_slot.py threshold so
# the per-process reservation and the host-wide semaphore agree on which
# decodes are the expensive ones.
LONG_PERIOD_SECONDS = 900


# Sentinel pushed once per worker to unblock the take loop at shutdown.
_SHUTDOWN = object()


class PriorityDecodePool:
    """Thread pool that runs shorter-period decodes before longer ones.

    Args:
        max_workers: total worker threads.
        max_long_inflight: max long (>= LONG_PERIOD_SECONDS) jobs that may
            run concurrently.  The rest of the workers stay available for
            short jobs.  Clamped to ``[1, max_workers]``.
        thread_name_prefix: prefix for worker thread names.
    """

    def __init__(
        self,
        max_workers: int,
        *,
        max_long_inflight: Optional[int] = None,
        thread_name_prefix: str = "decode",
    ) -> None:
        self._max_workers = max(1, int(max_workers))
        if max_long_inflight is None:
            # Default: leave at least one worker for short decodes; never
            # let long jobs take more than half the pool.
            max_long_inflight = max(1, self._max_workers // 2)
        self._max_long_inflight = max(1, min(int(max_long_inflight), self._max_workers))

        # Two heaps under one lock/condition.  Short jobs are strictly
        # preferred, so an idle worker always empties ``_short`` before
        # it will even look at ``_long``.  Each entry is
        # ``(period_seconds, seq, fn, args, kwargs, future)``; ``seq`` is
        # a monotonic counter giving FIFO order within an equal period.
        self._short: List[Tuple] = []
        self._long: List[Tuple] = []
        self._seq = 0
        self._long_inflight = 0
        self._shutdown = False

        self._cond = threading.Condition()

        self._workers: List[threading.Thread] = []
        for i in range(self._max_workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"{thread_name_prefix}_{i}",
                daemon=True,
            )
            t.start()
            self._workers.append(t)

    # -- public ThreadPoolExecutor-compatible surface ---------------------

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> Future:
        """Schedule ``fn(*args, **kwargs)``; returns a Future.

        Priority comes from ``args[0].period_seconds`` when present (the
        DecodeRequest); jobs without it sort as shortest (run ASAP).
        """
        future: Future = Future()
        period = self._period_of(args)
        with self._cond:
            if self._shutdown:
                raise RuntimeError("submit after shutdown")
            self._seq += 1
            entry = (period, self._seq, fn, args, kwargs, future)
            if period >= LONG_PERIOD_SECONDS:
                heapq.heappush(self._long, entry)
            else:
                heapq.heappush(self._short, entry)
            # One waiter is enough to pick up one new job.
            self._cond.notify()
        return future

    def pending(self) -> int:
        """Jobs queued but not yet picked up by a worker."""
        with self._cond:
            return len(self._short) + len(self._long)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._cond:
            self._shutdown = True
            if cancel_futures:
                for _, _, _, _, _, fut in self._short + self._long:
                    fut.cancel()
                self._short.clear()
                self._long.clear()
            # Wake every worker so each can observe shutdown and exit.
            self._cond.notify_all()
        if wait:
            for t in self._workers:
                t.join()

    @property
    def max_workers(self) -> int:
        return self._max_workers

    # -- internals --------------------------------------------------------

    @staticmethod
    def _period_of(args: tuple) -> int:
        if args:
            p = getattr(args[0], "period_seconds", None)
            if isinstance(p, int):
                return p
        return 0

    def _take_next(self):
        """Block until a runnable job is available; return its entry.

        Short jobs are always runnable.  A long job is runnable only
        while ``_long_inflight < _max_long_inflight`` — otherwise it
        stays queued and the worker waits (for a new short job, or for a
        running long job to finish and free a long slot).  Returns
        ``_SHUTDOWN`` when the pool is shutting down and no runnable work
        remains for this worker.
        """
        with self._cond:
            while True:
                if self._short:
                    entry = heapq.heappop(self._short)
                    return entry
                if self._long and self._long_inflight < self._max_long_inflight:
                    entry = heapq.heappop(self._long)
                    self._long_inflight += 1
                    return entry
                if self._shutdown:
                    # Drain remaining short work above first; once short is
                    # empty (and long is either empty or slot-capped) we
                    # let workers exit.  cancel_futures=False keeps any
                    # capped long jobs — but at shutdown we don't strand
                    # them: run them past the cap so wait=True can drain.
                    if self._long:
                        entry = heapq.heappop(self._long)
                        self._long_inflight += 1
                        return entry
                    return _SHUTDOWN
                self._cond.wait()

    def _worker_loop(self) -> None:
        while True:
            entry = self._take_next()
            if entry is _SHUTDOWN:
                return
            period, _seq, fn, args, kwargs, future = entry
            is_long = period >= LONG_PERIOD_SECONDS
            if not future.set_running_or_notify_cancel():
                # Future was cancelled before we started it.
                if is_long:
                    self._release_long()
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 — mirror Future semantics
                future.set_exception(exc)
                # band_recorder ignores the Future, so surface a breadcrumb.
                logger.debug("decode job raised: %s", exc, exc_info=True)
            else:
                future.set_result(result)
            finally:
                if is_long:
                    self._release_long()

    def _release_long(self) -> None:
        with self._cond:
            if self._long_inflight > 0:
                self._long_inflight -= 1
            # A freed long slot may make a queued long job runnable.
            self._cond.notify()


def build_decode_pool() -> "PriorityDecodePool":
    """Construct the pool sized from CPU affinity + env overrides.

    Worker count: ``WSPR_DECODE_WORKERS`` if set, else ALL the CPUs in
    this process's affinity set.  The systemd drop-in already pins the
    recorder off radiod's reserved cores, so the affinity set is exactly
    "the CPUs we're allowed to use" — and with no other significant load
    on the host there's no reason to leave any idle; we want every
    2-minute decode in a cycle to finish well inside the 110 s window.
    Long-decode concurrency: ``WSPR_DECODE_LONG_SLOTS`` if set, else half
    the workers — the rest stay reserved for short decodes.  Per-job
    memory is bounded elsewhere — F15/F30 slices are deferred until the
    host-wide slot is acquired (see host_slot.py / band_recorder.py) —
    so the worker count drives CPU use, not peak RSS.
    """
    try:
        allowed = len(os.sched_getaffinity(0))
    except AttributeError:                       # non-Linux
        allowed = os.cpu_count() or 4
    workers_env = os.environ.get("WSPR_DECODE_WORKERS")
    if workers_env:
        workers = max(2, int(workers_env))
    else:
        workers = max(2, allowed)
    long_env = os.environ.get("WSPR_DECODE_LONG_SLOTS")
    long_slots = int(long_env) if long_env else None
    pool = PriorityDecodePool(
        workers,
        max_long_inflight=long_slots,
        thread_name_prefix="wav_decoder",
    )
    logger.info(
        "decode pool: %d workers, <=%d concurrent long (>=%ds) decodes; "
        "short periods run first",
        pool.max_workers, pool._max_long_inflight, LONG_PERIOD_SECONDS,
    )
    return pool
