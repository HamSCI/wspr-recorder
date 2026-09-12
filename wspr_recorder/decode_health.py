"""Decode-health ledger — which bands fell permanently behind, and which cycles were lost.

A single decode running longer than its cycle is NOT a fault, and this
recorder is built so that it isn't one.  A decode job is triggered by the
audio being *available*, never by a wall clock: ``band_recorder`` emits a
slot the moment ``leading_off >= start_off + n_samples`` and walks its
``_slot_next_utc`` cursor forward one period at a time, so a late emitter
emits every pending slot in order; ``_dispatch_request`` then hands the job
to the pool without blocking, and ``PriorityDecodePool`` runs short periods
ahead of long ones.  The :00 and :30 boundaries, where F5 + F15 + F30 all
come due together across every band, are exactly the case that design
handles: a few late cycles, then back to normal.

What IS a fault is falling behind and staying there.  So this ledger
reports:

  BEHIND     a band has been at least ``late_s`` (120 s, one W2 cycle)
             behind for ``sustain_cycles`` (5) decodes in a row AND has
             made no net progress over that run.  Logged ERROR once per
             episode, with a reminder every ``sustain_cycles`` after that.
             A burst which is draining — lateness falling cycle over cycle
             — never trips it, however late it started, because the band is
             already fixing itself.
  CAUGHT_UP  a late run ended by itself: how long it lasted and how far
             behind it got.  A transient leaves a record, never an error.
  KILLED     wsprd / jt9 was killed by its timeout before it finished, so
             that mode reported no spots.  A definite lost cycle, ERROR.
  DROPPED    a cycle never decoded at all — its audio was no longer
             resident in the ring when the decode came up.  Also ERROR.
  LATE       one decode started a cycle or more after its audio was
             complete.  Recorded, never alarmed on its own: it is the raw
             material the BEHIND rule is computed from.

``late`` — the seconds between the end of a slot's audio and the start of
its decode — is the backlog measured in seconds, per band, and it is the
one number that separates a spike from a slide.  ``BacklogMonitor`` watches
the pool queue and says "falling behind right now"; this ledger says
whether that ever cost anything.

wsprdaemon's wd-decode-health.sh carries the same rule and the same names,
so one report reads the same on either system.

Exposed through ``wspr-ctl decode-health`` (and ``status`` / ``health``),
and appended to ``decode-health.jsonl`` in the state directory so the
history survives a restart and is readable when the daemon is down.

Env: WSPR_DECODE_HEALTH=0 disables; WSPR_DECODE_LATE_SEC (default 120) and
WSPR_DECODE_SUSTAIN_CYCLES (default 5) set the rule;
WSPR_DECODE_HEALTH_FILE overrides the event-log path.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_LATE_S = 120.0
DEFAULT_SUSTAIN_CYCLES = 5
MAX_EVENTS_KEPT = 500          # in-memory ring of recent events
MAX_LOG_BYTES = 2_000_000      # trim the JSONL at this size
OK_HEARTBEAT_S = 3600.0        # store at most one healthy decode per band per hour


def _state_dir() -> Path:
    return Path(os.environ.get("STATE_DIRECTORY", "/var/lib/wspr-recorder"))


@dataclass
class DecodeEvent:
    utc: str            # ISO-8601 Zulu, when the outcome was recorded
    status: str         # OK | LATE | BEHIND | CAUGHT_UP | KILLED | DROPPED
    band: str
    mode: str           # W2 / F2 / F5 / F15 / F30, or "-" for DROPPED
    period_s: int
    cycle_utc: str      # start of the slot whose audio this was
    late_s: float       # seconds from "audio complete" to "decode started"
    elapsed_s: float    # how long the decode ran; -1 when it never ran
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Streak:
    """One band's run of consecutive late decodes."""
    count: int = 0
    first_late_s: float = 0.0
    max_late_s: float = 0.0
    started_at: float = 0.0
    reported: bool = False


class DecodeHealth:
    """Thread-safe ledger.  Decodes run on pool threads, so every mutation
    takes the lock; the report methods are called from the asyncio thread
    serving IPC."""

    def __init__(self, *, late_s: Optional[float] = None,
                 sustain_cycles: Optional[int] = None,
                 event_log: Optional[Path] = None,
                 enabled: Optional[bool] = None) -> None:
        self.enabled = (os.environ.get("WSPR_DECODE_HEALTH", "1") != "0"
                        if enabled is None else enabled)
        self.late_s = late_s if late_s is not None else _env_float("WSPR_DECODE_LATE_SEC", DEFAULT_LATE_S)
        self.sustain_cycles = (sustain_cycles if sustain_cycles is not None
                               else _env_int("WSPR_DECODE_SUSTAIN_CYCLES", DEFAULT_SUSTAIN_CYCLES))
        if event_log is not None:
            self.event_log: Optional[Path] = event_log
        else:
            env_path = os.environ.get("WSPR_DECODE_HEALTH_FILE")
            self.event_log = Path(env_path) if env_path else _state_dir() / "decode-health.jsonl"
        self._lock = threading.Lock()
        self._events: Deque[DecodeEvent] = deque(maxlen=MAX_EVENTS_KEPT)
        self._counts: Dict[str, int] = {}
        self._band_last: Dict[str, DecodeEvent] = {}
        self._band_slowest: Dict[str, float] = {}
        self._band_decodes: Dict[str, int] = {}
        self._last_ok_stored: Dict[str, float] = {}
        self._streaks: Dict[str, _Streak] = {}
        self._log_broken = False

    # ── recording ───────────────────────────────────────────────────────
    def record(self, *, band: str, mode: str, period_s: int,
               cycle_start: float, decode_start: float, elapsed_s: float,
               killed: bool = False) -> DecodeEvent:
        """One decode finished (or was killed).  Times are UNIX epochs;
        ``cycle_start`` is the wall-clock start of the audio slice, so the
        audio was complete at ``cycle_start + period_s``.

        A slow decode is recorded but never alarmed; only a sustained run of
        late starts with no net progress raises BEHIND."""
        late_s = max(0.0, decode_start - (cycle_start + period_s))
        cycle_utc = _iso(cycle_start)

        if killed:
            # A killed decoder lost that cycle outright, whatever the streak does.
            return self._add(DecodeEvent(
                utc=_iso(time.time()), status="KILLED", band=band, mode=mode,
                period_s=int(period_s), cycle_utc=cycle_utc,
                late_s=round(late_s, 1), elapsed_s=round(elapsed_s, 1),
                detail="killed by its decode timeout"))

        with self._lock:
            streak = self._streaks.setdefault(band, _Streak())
            self._band_slowest[band] = max(self._band_slowest.get(band, 0.0), elapsed_s)
            self._band_decodes[band] = self._band_decodes.get(band, 0) + 1

            if late_s < self.late_s:
                if streak.count == 0:
                    return self._add_locked(DecodeEvent(
                        utc=_iso(time.time()), status="OK", band=band, mode=mode,
                        period_s=int(period_s), cycle_utc=cycle_utc,
                        late_s=round(late_s, 1), elapsed_s=round(elapsed_s, 1)))
                # We were behind and are not any more: the band drained itself.
                mins = (time.time() - streak.started_at) / 60.0
                event = DecodeEvent(
                    utc=_iso(time.time()), status="CAUGHT_UP", band=band, mode=mode,
                    period_s=int(period_s), cycle_utc=cycle_utc,
                    late_s=round(late_s, 1), elapsed_s=round(elapsed_s, 1),
                    detail=(f"caught up after {streak.count} late cycles over "
                            f"{mins:.0f} min, worst {streak.max_late_s:.0f}s behind"))
                was_reported = streak.reported
                self._streaks[band] = _Streak()
                return self._add_locked(event, escalate=was_reported)

            # At least one cycle late
            if streak.count == 0:
                streak.first_late_s = late_s
                streak.started_at = time.time()
                streak.max_late_s = 0.0
            streak.count += 1
            streak.max_late_s = max(streak.max_late_s, late_s)
            draining = late_s < streak.first_late_s

            if streak.count < self.sustain_cycles or draining:
                return self._add_locked(DecodeEvent(
                    utc=_iso(time.time()), status="LATE", band=band, mode=mode,
                    period_s=int(period_s), cycle_utc=cycle_utc,
                    late_s=round(late_s, 1), elapsed_s=round(elapsed_s, 1),
                    detail=(f"late cycle {streak.count} of this run, started at "
                            f"{streak.first_late_s:.0f}s, draining={draining}")))

            # Sustained, and no net progress since the run began
            first_report = not streak.reported
            streak.reported = True
            if first_report or streak.count % self.sustain_cycles == 0:
                return self._add_locked(DecodeEvent(
                    utc=_iso(time.time()), status="BEHIND", band=band, mode=mode,
                    period_s=int(period_s), cycle_utc=cycle_utc,
                    late_s=round(late_s, 1), elapsed_s=round(elapsed_s, 1),
                    detail=(f"{streak.count} late cycles with no progress, "
                            f"{streak.first_late_s:.0f}s -> {late_s:.0f}s")))
            return self._add_locked(DecodeEvent(
                utc=_iso(time.time()), status="LATE", band=band, mode=mode,
                period_s=int(period_s), cycle_utc=cycle_utc,
                late_s=round(late_s, 1), elapsed_s=round(elapsed_s, 1),
                detail=f"late cycle {streak.count} of this run, already reported BEHIND"))

    def record_drop(self, *, band: str, period_s: int, cycle_start: float,
                    detail: str) -> DecodeEvent:
        """A cycle that was never decoded at all."""
        return self._add(DecodeEvent(
            utc=_iso(time.time()), status="DROPPED", band=band, mode="-",
            period_s=int(period_s), cycle_utc=_iso(cycle_start),
            late_s=round(max(0.0, time.time() - (cycle_start + period_s)), 1),
            elapsed_s=-1.0, detail=detail))

    def _add(self, event: DecodeEvent) -> DecodeEvent:
        with self._lock:
            return self._add_locked(event)

    def _add_locked(self, event: DecodeEvent, *, escalate: bool = False) -> DecodeEvent:
        """Store, then log.  Caller holds the lock.

        A healthy decode is stored at most once per hour per band: a fleet
        decoding 14 bands every 2 minutes would otherwise push every episode
        out of the ring (and out of the JSONL) within the hour, which is
        exactly the history an operator comes here for.  The band's latest
        standing is tracked on every decode regardless, so the report can
        still show a healthy band as healthy."""
        if not self.enabled:
            return event
        if event.status != "DROPPED":
            self._band_last[event.band] = event
        self._counts[event.status] = self._counts.get(event.status, 0) + 1
        if event.status == "OK":
            now = time.time()
            if now - self._last_ok_stored.get(event.band, 0.0) < OK_HEARTBEAT_S:
                return event                     # healthy, and we said so recently
            self._last_ok_stored[event.band] = now
        self._events.append(event)
        self._log(event, escalate=escalate)
        self._append_log(event)
        return event

    def _log(self, event: DecodeEvent, *, escalate: bool) -> None:
        if event.status == "BEHIND":
            logger.error(
                "%s: DECODES ARE FALLING BEHIND — %s (%.0fs, %.1f cycles). "
                "This band will not catch up on its own: it needs more CPU or fewer "
                "modes.  See 'wspr-ctl decode-health'",
                event.band, event.detail, event.late_s, event.late_s / 120.0)
        elif event.status == "KILLED":
            logger.error(
                "%s %s: DECODE KILLED after %.0fs — the %ds slot starting %s reported "
                "no spots", event.band, event.mode, event.elapsed_s,
                event.period_s, event.cycle_utc)
        elif event.status == "DROPPED":
            logger.error("%s: CYCLE DROPPED — %s (slot %s, %ds)",
                         event.band, event.detail, event.cycle_utc, event.period_s)
        elif event.status == "CAUGHT_UP":
            # Only worth an operator's attention if we had cried BEHIND about it.
            (logger.warning if escalate else logger.debug)(
                "%s: %s — now %.0fs behind", event.band, event.detail, event.late_s)
        else:
            logger.debug("%s %s: %s late=%.0fs elapsed=%.0fs",
                         event.band, event.mode, event.status, event.late_s, event.elapsed_s)

    def _append_log(self, event: DecodeEvent) -> None:
        if self.event_log is None or self._log_broken:
            return
        try:
            self.event_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.event_log, "a") as fh:
                fh.write(json.dumps(event.as_dict()) + "\n")
            if self.event_log.stat().st_size > MAX_LOG_BYTES:
                self._trim_log()
        except OSError as exc:
            # A read-only / absent state dir must never break decoding; say so once.
            self._log_broken = True
            logger.warning("decode-health: cannot write %s (%s) — "
                           "keeping the ledger in memory only", self.event_log, exc)

    def _trim_log(self) -> None:
        try:
            lines = self.event_log.read_text().splitlines()[-2000:]
            self.event_log.write_text("\n".join(lines) + "\n")
        except OSError:
            pass

    # ── reporting ───────────────────────────────────────────────────────
    def bands_behind(self) -> Dict[str, dict]:
        """Bands currently in a reported BEHIND episode."""
        with self._lock:
            return {b: {"late_cycles": s.count,
                        "first_late_s": round(s.first_late_s, 1),
                        "worst_late_s": round(s.max_late_s, 1),
                        "minutes": round((time.time() - s.started_at) / 60.0, 1)}
                    for b, s in self._streaks.items() if s.reported}

    def summary(self, window_s: float = 86400.0) -> dict:
        """Counts and per-band detail over the last ``window_s`` seconds,
        from the in-memory ring (the JSONL holds the longer history)."""
        cutoff = _iso(time.time() - window_s)
        with self._lock:
            events = [e for e in self._events if e.utc >= cutoff]
            lifetime = dict(self._counts)
            band_last = {b: e.as_dict() for b, e in self._band_last.items()}
            slowest = dict(self._band_slowest)
            decodes = dict(self._band_decodes)
        counts: Dict[str, int] = {}
        per_band: Dict[str, Dict[str, float]] = {}
        for e in events:
            counts[e.status] = counts.get(e.status, 0) + 1
            b = per_band.setdefault(e.band, {"BEHIND": 0, "KILLED": 0, "DROPPED": 0,
                                             "LATE": 0, "CAUGHT_UP": 0, "OK": 0,
                                             "worst_late_s": 0.0, "slowest_decode_s": 0.0})
            b[e.status] = b.get(e.status, 0) + 1
            b["worst_late_s"] = max(b["worst_late_s"], e.late_s)
            b["slowest_decode_s"] = slowest.get(e.band, 0.0)
        missed = counts.get("KILLED", 0) + counts.get("DROPPED", 0)
        return {
            "enabled": self.enabled,
            "window_hours": round(window_s / 3600.0, 1),
            "rule": {"late_s": self.late_s, "sustain_cycles": self.sustain_cycles},
            "counts": counts,
            "lifetime_counts": lifetime,
            "decodes_total": sum(decodes.values()),
            "cycles_missed": missed,
            "bands_behind": self.bands_behind(),
            "per_band": {b: v for b, v in per_band.items()
                         if any(v[k] for k in ("BEHIND", "KILLED", "DROPPED"))},
            "last_per_band": band_last,
            "recent_problems": [e.as_dict() for e in events
                                if e.status in ("BEHIND", "KILLED", "DROPPED", "CAUGHT_UP")][-12:],
            "event_log": str(self.event_log) if self.event_log else None,
        }

    def issues(self, window_s: float = 3600.0) -> List[str]:
        """Lines for ``health``.  A band which is merely having a slow patch
        is not an issue; one which has stopped recovering is."""
        cutoff = _iso(time.time() - window_s)
        with self._lock:
            events = [e for e in self._events if e.utc >= cutoff]
        out = []
        behind = self.bands_behind()
        if behind:
            out.append("decodes falling behind and not recovering on: "
                       + ", ".join(f"{b} ({v['worst_late_s']:.0f}s, {v['late_cycles']} cycles)"
                                   for b, v in sorted(behind.items()))
                       + " — see 'wspr-ctl decode-health'")
        for status, text in (("KILLED", "decode(s) killed by their timeout — those cycles reported no spots"),
                             ("DROPPED", "cycle(s) never decoded at all")):
            n = sum(1 for e in events if e.status == status)
            if n:
                out.append(f"{n} {text} in the last {window_s / 3600:.0f} h "
                           f"(see 'wspr-ctl decode-health')")
        return out


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


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# Module-level singleton: the decoders run deep inside decoder.py and have
# no handle on the daemon object, so they reach the ledger through here.
HEALTH = DecodeHealth()


def read_event_log(path: Optional[Path] = None, limit: int = 2000) -> List[dict]:
    """Read the persisted ledger — used by `wspr-ctl decode-health` when the
    daemon is not running, so the history is still readable after a crash."""
    p = path or (Path(os.environ["WSPR_DECODE_HEALTH_FILE"])
                 if os.environ.get("WSPR_DECODE_HEALTH_FILE")
                 else _state_dir() / "decode-health.jsonl")
    try:
        lines = p.read_text().splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
