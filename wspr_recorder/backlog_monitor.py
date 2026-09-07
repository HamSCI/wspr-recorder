"""Decode-backlog monitor — notice when the decoders stop keeping up.

wsprdaemon added the equivalent (wd-decode-backlog.sh) after K6FOD's decoder,
starved by a 1.4 GHz clock cap, silently built a 42 GB / 3600-file FT8 pile-up
that nothing watched.  Here the decoders run in-process on a priority pool, so
the pile-up is a queue, and the two signals are:

  stuck    the oldest queued job has waited longer than ``warn_wait_s``.  A
           job that waits a whole W2 period means one cycle's work was not
           drained within a cycle, so the queue can only grow from here.
  growing  the queue is at least ``floor`` deep and has grown, without ever
           shrinking, across the last ``grow_samples`` samples (5 min at the
           60 s cadence).  Catches the slower slide before it gets stuck.

A short spike at :00/:30, when every cadence emits at once, is normal and
trips neither rule.  Assessment transitions are logged (WARNING on entry,
one reminder every ``remind_s`` while it persists, INFO on recovery) and the
latest assessment is published in status.json / ``wspr-ctl health``.

Thresholds: WSPR_BACKLOG_WARN_WAIT_SEC (default 120), WSPR_BACKLOG_FLOOR
(default: the pool's worker count), WSPR_BACKLOG_GROW_SAMPLES (default 5).
"""
from __future__ import annotations

import logging
import os
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

logger = logging.getLogger(__name__)

DEFAULT_WARN_WAIT_S = 120.0
DEFAULT_GROW_SAMPLES = 5
DEFAULT_REMIND_S = 600.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Assessment:
    level: str          # "ok" | "warn"
    reason: str
    queued: int
    oldest_wait_s: float
    running: int

    def as_dict(self) -> dict:
        return {"level": self.level, "reason": self.reason, "queued": self.queued,
                "oldest_wait_s": round(self.oldest_wait_s, 1), "running": self.running}


class BacklogMonitor:
    """Feed it one pool snapshot per sample; it judges and logs transitions."""

    def __init__(self, workers: int, *, warn_wait_s: Optional[float] = None,
                 floor: Optional[int] = None, grow_samples: Optional[int] = None,
                 remind_s: float = DEFAULT_REMIND_S, log: logging.Logger = logger) -> None:
        self.warn_wait_s = warn_wait_s if warn_wait_s is not None else \
            _env_float("WSPR_BACKLOG_WARN_WAIT_SEC", DEFAULT_WARN_WAIT_S)
        self.floor = floor if floor is not None else _env_int("WSPR_BACKLOG_FLOOR", max(1, workers))
        self.grow_samples = grow_samples if grow_samples is not None else \
            _env_int("WSPR_BACKLOG_GROW_SAMPLES", DEFAULT_GROW_SAMPLES)
        self.remind_s = remind_s
        self._log = log
        self._history: Deque[int] = deque(maxlen=max(2, self.grow_samples + 1))
        self._last: Optional[Assessment] = None
        self._warn_since: Optional[float] = None
        self._last_logged: float = 0.0

    # ── pure judgement ──────────────────────────────────────────────────────
    def assess(self, snapshot: dict) -> Assessment:
        queued = int(snapshot.get("queued", 0))
        oldest = float(snapshot.get("oldest_wait_s", 0.0))
        running = int(snapshot.get("running", 0))
        self._history.append(queued)
        if queued and oldest > self.warn_wait_s:
            return Assessment("warn", f"decode backlog STUCK: oldest of {queued} queued decode(s) "
                                      f"has waited {oldest:.0f}s (> {self.warn_wait_s:.0f}s) — the "
                                      f"decoders are not keeping up with the cycle", queued, oldest, running)
        h = list(self._history)
        if (len(h) > self.grow_samples and queued >= self.floor
                and all(b >= a for a, b in zip(h, h[1:])) and h[-1] > h[-1 - self.grow_samples]):
            return Assessment("warn", f"decode backlog GROWING: {h[-1 - self.grow_samples]} → {queued} "
                                      f"queued over the last {self.grow_samples} samples and never "
                                      f"shrinking", queued, oldest, running)
        return Assessment("ok", f"{queued} queued, oldest {oldest:.0f}s, {running} running", queued, oldest, running)

    # ── sampling with transition logging ────────────────────────────────────
    def observe(self, snapshot: dict, now: float) -> Assessment:
        a = self.assess(snapshot)
        was_warn = self._last is not None and self._last.level == "warn"
        if a.level == "warn":
            if not was_warn:
                self._warn_since = now
                self._last_logged = now
                self._log.warning("%s (workers=%s)", a.reason, snapshot.get("workers"))
            elif now - self._last_logged >= self.remind_s:
                self._last_logged = now
                self._log.warning("%s — persisting for %.0f min", a.reason, (now - (self._warn_since or now)) / 60)
        elif was_warn:
            self._log.info("decode backlog cleared after %.0f min: %s",
                           (now - (self._warn_since or now)) / 60, a.reason)
            self._warn_since = None
        self._last = a
        return a

    @property
    def last(self) -> Optional[Assessment]:
        return self._last

    def as_dict(self) -> dict:
        return self._last.as_dict() if self._last else {"level": "unknown", "reason": "not sampled yet"}
