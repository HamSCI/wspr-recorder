"""Cycle-dt timing guard — external-truth re-anchor trigger.

WSPR transmitters are UTC-aligned, so the per-cycle average decoder dt
(already computed for the cycle log line) measures the recorder's
slot-anchor offset from true UTC — a reference no stale or self-consistent
radiod mapping can fool.

The internal guards can go blind in exactly the failure they exist to
catch: the abs-divergence check measures against the same
StatusListener-refreshed ChannelInfo that places the windows (a frozen or
drift-tracking feed reads as zero divergence), sub-threshold steps
accumulate without ever tripping the 0.75 s gate, and ka9q-python removed
the anchor_epoch step detection on 2026-06-28 (the field is vestigial, so
the epoch watcher can never fire).  Observed 2026-07-16 on B4-100: radiod
output steps walked wspr dt from +0.2 s to -1.9 s across 10 minutes with
zero faults raised, while FT8 (freshly re-anchored) stayed clean.

This module is the pure, unit-testable strike logic; WsprRecorder wires
it to CycleBatcher's on_cycle_dt hook and, on fire, re-anchors every band
recorder of the offending rx against its current ChannelState (the same
recovery as the stream-restored path).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

# Env knobs (read once by from_env): WSPR_DT_GUARD_SEC (0 disables),
# WSPR_DT_GUARD_MIN_SPOTS, WSPR_DT_GUARD_CYCLES.


@dataclass
class DtGuardConfig:
    # |avg dt| beyond this = misplaced window.  wsprd tolerates roughly
    # +/-2 s, so 1.25 s recovers while decodes are still (partially)
    # landing, and sits above the benign 0.25-0.31 s output jitter of a
    # busy radiod plus normal decode scatter.
    threshold_sec: float = 1.25
    # Need a real population for the average — transmitters' own timing
    # errors cancel across many spots, but a lone rogue TX must not
    # trigger a fleet-wide re-anchor.
    min_spots: int = 5
    # Consecutive offending cycles before firing (a one-off decode
    # oddity passes; a real anchor fault persists every cycle).
    cycles: int = 2

    @classmethod
    def from_env(cls) -> Optional["DtGuardConfig"]:
        """Build from env; returns None when disabled (WSPR_DT_GUARD_SEC=0)."""
        try:
            threshold = float(os.environ.get("WSPR_DT_GUARD_SEC", "1.25"))
        except ValueError:
            threshold = 1.25
        if threshold <= 0:
            return None
        try:
            min_spots = int(os.environ.get("WSPR_DT_GUARD_MIN_SPOTS", "5"))
        except ValueError:
            min_spots = 5
        try:
            cycles = int(os.environ.get("WSPR_DT_GUARD_CYCLES", "2"))
        except ValueError:
            cycles = 2
        return cls(threshold_sec=threshold, min_spots=min_spots,
                   cycles=max(1, cycles))


def dt_guard_step(
    strikes: int,
    avg_dt: Optional[float],
    n_spots: int,
    cfg: DtGuardConfig,
) -> Tuple[int, bool]:
    """One guard step for one rx cycle -> (new_strikes, fire).

    Cycles with no dt or too few spots neither add strikes nor clear
    them — a low-activity cycle carries no evidence either way, and a
    genuine fault should not be forgiven by a quiet band-minute.
    A healthy cycle (|avg dt| within threshold, real population) clears
    the strikes.  Firing resets the count so recovery gets a clean slate.
    """
    if avg_dt is None or n_spots < cfg.min_spots:
        return strikes, False
    if abs(avg_dt) <= cfg.threshold_sec:
        return 0, False
    strikes += 1
    if strikes >= cfg.cycles:
        return 0, True
    return strikes, False


# ─── wall-clock slot guard ───────────────────────────────────────────────
#
# The dt guard above needs decoded spots to measure dt — in a GROSS anchor
# fault (minutes off) there are ZERO decodes, so it is blind exactly when
# the damage is worst.  Observed 2026-07-23 on B4: recorders anchored
# while radiod was still starting came up ~+10 min ahead; every WAV was
# labeled 10 min in the future, 0 spots for 20+ min, noise flowing, dt
# guard silent, and the fleet timing watchdog refused to act ("no peer
# decoding" — a single-instance host never has a peer).
#
# This guard needs no decodes.  Physics: a slot labeled T with period P
# can only FINISH filling at true wall time >= T+P — samples arrive in
# real time.  A complete slot harvested well BEFORE its nominal end is
# impossible unless the RTP→UTC anchor is ahead of true UTC.  (The
# behind-anchor direction closes slots late, which is indistinguishable
# from benign decode backlog at this hook — the dt guard owns that side
# once decodes trickle in.)  System clock is chrony-disciplined (the
# recorder refuses to start capture until chrony settles), so wall clock
# is the one ruler the radiod mapping cannot fool.


@dataclass
class WallClockGuardConfig:
    # A complete slot finishing more than this many seconds before its
    # nominal end time is physically impossible with a sane anchor.
    # Write/flush jitter is sub-second; 5 s is far above any of it and
    # far below any fault worth re-anchoring for.
    threshold_sec: float = 5.0
    # Consecutive impossible slots (across bands of one rx) before
    # firing.  All ~17 bands of a broken rx offend every cycle, so this
    # trips within one cycle while a lone glitch cannot.
    strikes: int = 3
    # Partial slots (shutdown flush, stream-gap harvest) may legitimately
    # close early — only near-complete slots carry evidence.
    min_completeness_pct: float = 90.0

    @classmethod
    def from_env(cls) -> Optional["WallClockGuardConfig"]:
        """Build from env; returns None when disabled
        (WSPR_WALLCLOCK_GUARD_SEC=0)."""
        try:
            threshold = float(
                os.environ.get("WSPR_WALLCLOCK_GUARD_SEC", "5.0"))
        except ValueError:
            threshold = 5.0
        if threshold <= 0:
            return None
        try:
            strikes = int(os.environ.get("WSPR_WALLCLOCK_GUARD_STRIKES", "3"))
        except ValueError:
            strikes = 3
        return cls(threshold_sec=threshold, strikes=max(1, strikes))


def wallclock_guard_step(
    strikes: int,
    early_by_sec: Optional[float],
    completeness_pct: float,
    cfg: WallClockGuardConfig,
) -> Tuple[int, bool]:
    """One guard step for one harvested slot -> (new_strikes, fire).

    ``early_by_sec`` is (slot_start + period) - now: positive means the
    slot completed before its nominal end — the impossible direction.
    Unknown timing or an incomplete slot is inert (no strike, no
    forgiveness).  A physically plausible slot clears the strikes.
    Firing resets the count so recovery gets a clean slate.
    """
    if early_by_sec is None or completeness_pct < cfg.min_completeness_pct:
        return strikes, False
    if early_by_sec <= cfg.threshold_sec:
        return 0, False
    strikes += 1
    if strikes >= cfg.strikes:
        return 0, True
    return strikes, False
