"""Tests for the cycle-dt external-truth timing guard (timing_guard.py)."""

from wspr_recorder.timing_guard import DtGuardConfig, dt_guard_step


CFG = DtGuardConfig(threshold_sec=1.25, min_spots=5, cycles=2)


def test_healthy_cycle_no_strike():
    assert dt_guard_step(0, 0.2, 40, CFG) == (0, False)


def test_healthy_cycle_clears_existing_strikes():
    assert dt_guard_step(1, -0.3, 40, CFG) == (0, False)


def test_offending_cycle_accumulates_then_fires():
    strikes, fire = dt_guard_step(0, -1.9, 40, CFG)
    assert (strikes, fire) == (1, False)
    strikes, fire = dt_guard_step(strikes, -1.8, 35, CFG)
    assert fire
    assert strikes == 0  # clean slate for post-recovery evaluation


def test_positive_offsets_count_too():
    strikes, fire = dt_guard_step(1, 1.6, 20, CFG)
    assert fire


def test_low_population_is_inert_both_ways():
    # Too few spots: neither adds a strike...
    assert dt_guard_step(0, -1.9, 3, CFG) == (0, False)
    # ...nor forgives one (a quiet band-minute is not evidence of health).
    assert dt_guard_step(1, -0.1, 2, CFG) == (1, False)


def test_missing_dt_is_inert():
    assert dt_guard_step(1, None, 50, CFG) == (1, False)


def test_boundary_value_is_healthy():
    # Exactly at the threshold does not strike (<= is healthy).
    assert dt_guard_step(1, 1.25, 40, CFG) == (0, False)


def test_single_cycle_config_fires_immediately():
    cfg = DtGuardConfig(threshold_sec=1.0, min_spots=1, cycles=1)
    strikes, fire = dt_guard_step(0, 1.5, 1, cfg)
    assert fire


def test_from_env_disable(monkeypatch):
    monkeypatch.setenv("WSPR_DT_GUARD_SEC", "0")
    assert DtGuardConfig.from_env() is None


def test_from_env_defaults(monkeypatch):
    monkeypatch.delenv("WSPR_DT_GUARD_SEC", raising=False)
    monkeypatch.delenv("WSPR_DT_GUARD_MIN_SPOTS", raising=False)
    monkeypatch.delenv("WSPR_DT_GUARD_CYCLES", raising=False)
    cfg = DtGuardConfig.from_env()
    assert cfg is not None
    assert cfg.threshold_sec == 1.25
    assert cfg.min_spots == 5
    assert cfg.cycles == 2


# ─── wall-clock slot guard ───────────────────────────────────────────────

from wspr_recorder.timing_guard import (
    WallClockGuardConfig, wallclock_guard_step,
)

WCFG = WallClockGuardConfig(threshold_sec=5.0, strikes=3,
                            min_completeness_pct=90.0)


def test_wallclock_plausible_slot_no_strike():
    # Finished right at its nominal end (small negative = finished after).
    assert wallclock_guard_step(0, -1.2, 100.0, WCFG) == (0, False)


def test_wallclock_late_slot_clears_strikes():
    # Decode backlog: finished well after nominal end — plausible, clears.
    assert wallclock_guard_step(2, -240.0, 100.0, WCFG) == (0, False)


def test_wallclock_impossible_slots_accumulate_then_fire():
    # The 2026-07-23 B4 incident: anchor +10 min ahead, slots complete
    # ~600 s before their nominal end, every band, every cycle.
    strikes, fire = wallclock_guard_step(0, 598.0, 100.0, WCFG)
    assert (strikes, fire) == (1, False)
    strikes, fire = wallclock_guard_step(strikes, 601.0, 100.0, WCFG)
    assert (strikes, fire) == (2, False)
    strikes, fire = wallclock_guard_step(strikes, 597.0, 100.0, WCFG)
    assert fire
    assert strikes == 0  # clean slate for post-recovery evaluation


def test_wallclock_partial_slot_is_inert_both_ways():
    # Shutdown flush / stream-gap harvest close early with gaps: neither
    # a strike...
    assert wallclock_guard_step(0, 90.0, 40.0, WCFG) == (0, False)
    # ...nor forgiveness (an incomplete slot is not evidence of health).
    assert wallclock_guard_step(2, -1.0, 40.0, WCFG) == (2, False)


def test_wallclock_unknown_timing_is_inert():
    assert wallclock_guard_step(1, None, 100.0, WCFG) == (1, False)


def test_wallclock_jitter_within_threshold_clears():
    assert wallclock_guard_step(2, 3.9, 100.0, WCFG) == (0, False)


def test_wallclock_env_disable():
    import os
    os.environ["WSPR_WALLCLOCK_GUARD_SEC"] = "0"
    try:
        assert WallClockGuardConfig.from_env() is None
    finally:
        del os.environ["WSPR_WALLCLOCK_GUARD_SEC"]


# --- evidence for the RecoveryLadder ------------------------------------
#
# ⛔ AC0G-ND, 2026-09-03.  The dt-guard caught a slot anchor eight minutes off
# true UTC, re-anchored all 17 bands, faulted again two cycles later,
# re-anchored, faulted — and would have continued all night.  It counts
# STRIKES toward firing and never counts FIRES, so nothing could conclude that
# re-anchoring was not working.  Every liveness check stayed green throughout:
# the recorder completed 120 s slots at "100% complete" and decoded nothing.
#
# Escalating needs a per-cycle verdict, and `not fire` is the wrong one:
# during a persistent fault the guard fires on every SECOND cycle (it needs
# `cycles` consecutive offenders, then resets for a clean slate), so the
# in-between cycles are offending but silent.  Reading those as healthy would
# clear the ladder every time and the escalation could never arrive.

from wspr_recorder.timing_guard import dt_guard_evidence  # noqa: E402


def _cfg(threshold=1.0, cycles=2, min_spots=3):
    return DtGuardConfig(threshold_sec=threshold, cycles=cycles,
                         min_spots=min_spots)


def test_a_fired_cycle_is_a_fault():
    assert dt_guard_evidence(+8.0, 10, _cfg(), fired=True) is False


def test_a_clean_cycle_with_a_real_population_is_healthy():
    assert dt_guard_evidence(+0.2, 10, _cfg(), fired=False) is True


def test_a_quiet_band_minute_carries_no_evidence():
    # The guard's own rule: "a genuine fault should not be forgiven by a
    # quiet band-minute."  The ladder must not be cleared by one either.
    assert dt_guard_evidence(+0.2, 1, _cfg(), fired=False) is None
    assert dt_guard_evidence(None, 10, _cfg(), fired=False) is None


def test_an_unfired_STRIKE_is_not_healthy():
    # ⛔ THE case that makes escalation possible.  This cycle is offending
    # (dt beyond threshold) but has not yet fired.  Calling it healthy would
    # reset the ladder on every second cycle of a persistent fault.
    assert dt_guard_evidence(+8.0, 10, _cfg(), fired=False) is None


def test_a_persistent_fault_reaches_the_restart():
    """End to end over the guard's real firing pattern.

    Two offending cycles fire, the count resets, two more fire again — so the
    ladder sees fault, (nothing), fault and reaches its second rung.  With
    120 s WSPR cycles that is roughly eight minutes to self-repair, against
    never before.
    """
    from ka9q.recovery_ladder import RecoveryAction, RecoveryLadder

    cfg = _cfg()
    ladder = RecoveryLadder(reprovision_after=1, full_reset_after=2,
                            restart_after=2)
    strikes = 0
    actions = []
    for _ in range(4):                      # four consecutive offending cycles
        strikes, fire = dt_guard_step(strikes, +8.0, 10, cfg)
        ev = dt_guard_evidence(+8.0, 10, cfg, fired=fire)
        actions.append(None if ev is None else ladder.observe(healthy=ev))

    assert actions == [None,
                       RecoveryAction.REPROVISION,
                       None,
                       RecoveryAction.RESTART_SELF], actions


def test_recovery_before_the_second_fire_forecloses_the_restart():
    # ⛔ Safety.  Re-anchoring that WORKS must not be followed by a restart:
    # killing a recovered recorder is worse than the fault it answered.
    from ka9q.recovery_ladder import RecoveryAction, RecoveryLadder

    cfg = _cfg()
    ladder = RecoveryLadder(reprovision_after=1, full_reset_after=2,
                            restart_after=2)
    strikes = 0
    for dt in (+8.0, +8.0):                 # fires on the second
        strikes, fire = dt_guard_step(strikes, dt, 10, cfg)
        ev = dt_guard_evidence(dt, 10, cfg, fired=fire)
        if ev is not None:
            act = ladder.observe(healthy=ev)
    assert act is RecoveryAction.REPROVISION

    # the re-anchor worked: a clean cycle clears everything
    strikes, fire = dt_guard_step(strikes, +0.1, 10, cfg)
    ev = dt_guard_evidence(+0.1, 10, cfg, fired=fire)
    assert ladder.observe(healthy=ev) is RecoveryAction.NONE
    assert ladder.consecutive_degraded == 0
