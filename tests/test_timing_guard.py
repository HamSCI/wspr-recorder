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
