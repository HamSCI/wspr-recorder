"""kiwisdr [[source]] wiring in __main__ (Phase 4c, Task 5).

A Kiwi source gets a KiwiSourceManager; each Kiwi band is timed from the
host clock (never an injected hf-timestd T-tier); the radiod-only recovery
ladder never touches a Kiwi manager.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

import numpy as np
import pytest

from wspr_recorder.__main__ import WsprRecorder
from wspr_recorder.band_recorder import DecodeRequest
from wspr_recorder.config import Config, load_config
from wspr_recorder.decode_mode import DecodeMode
from wspr_recorder.kiwi_source import KiwiSourceManager
from wspr_recorder.receiver_manager import ReceiverManager
from wspr_recorder.timing_service import TimingService
from wspr_recorder.wav_writer import WavWriter

F20, F30 = 14095600, 10138700
KIWI = "kiwisdr:10.22.23.70:8073"
RADIOD = "radiod:bee1-hf-status.local"

TOML = f'''
[[source]]
status = "bee1-hf-status.local"

[[source]]
kiwisdr = "10.22.23.70:8073"
channels = 8

[[band]]
frequency = {F20}
modes = ["W2"]

[[band]]
frequency = {F30}
modes = ["W2"]
'''


def _config(text=TOML):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "wspr-recorder.toml")
        with open(p, "w") as f:
            f.write(text)
        with mock.patch.object(Config, "validate", return_value=[]):
            return load_config(p)


@pytest.fixture
def t6_authority(monkeypatch):
    """hf-timestd publishes a usable T6 offset for the station's radiod."""
    snap = SimpleNamespace(
        offset_usable=True, rtp_to_utc_offset_ns=1_000_000,
        offset_seconds=0.001, t_level_active="T6", a_level="A3",
        sigma_ns=50, governor_radiod="bee1-hf-status.local",
        utc_published=datetime.now(timezone.utc), host_clock=None,
        to_timing_authority=lambda radiod=None: {
            "source": "hf-timestd", "t_level_active": "T6"},
    )

    class FakeReader:
        def read(self):
            return snap

    import hamsci_dsp.timing as hdt
    monkeypatch.setattr(hdt, "AuthorityReader", FakeReader)
    return snap


class FakeChannel:
    def __init__(self, host, port, freq_hz, *, password, user, on_audio,
                 on_state, behaviour="connect"):
        self.freq_hz = freq_hz
        self.on_audio, self.on_state = on_audio, on_state
        self.sample_rate = 12000.0
        self.behaviour = behaviour

    def start(self):
        if self.behaviour == "connect":
            self.on_state("connected", "sample_rate=12000")
        elif self.behaviour == "refuse":
            self.on_state("refused", "too_busy=1")
        elif self.behaviour == "badp":
            self.on_state("auth_failed", "badp=1")
        else:
            self.on_state("dropped", "connection refused")

    def stop(self):
        pass


def _recorder(config, executor=None):
    rec = object.__new__(WsprRecorder)
    rec.config = config
    rec.receiver_managers = {}
    rec.band_recorders = {}
    rec.cycle_batcher = None
    rec.executor = executor or MagicMock()
    rec._on_period_complete = MagicMock()
    rec.timing_service = TimingService(
        enable_chrony=False, enable_hf_timestd=False, authority="auto",
        client_radiod="bee1-hf-status.local")
    return rec


def _kiwi_manager(rec, src, behaviour="connect", channels=None,
                  start_supervisor=False):
    channels = [] if channels is None else channels

    def factory(*a, **kw):
        b = behaviour(a[2]) if callable(behaviour) else behaviour
        ch = FakeChannel(*a, behaviour=b, **kw)
        channels.append(ch)
        return ch

    mgr = KiwiSourceManager(
        rec.config,
        lambda ssrc, st, sk=src.key: rec._build_sink(ssrc, st, source_key=sk),
        src, None, channel_factory=factory,
        start_supervisor=start_supervisor)
    rec.receiver_managers[src.key] = mgr
    return mgr


def _feed(ch, seconds, frame=1200):
    for _ in range(int(seconds * 12000 / frame)):
        ch.on_audio(np.zeros(frame, np.float32), 0.0)


# --- Step 1 tests from the brief ------------------------------------------

def test_kiwisdr_source_builds_kiwi_manager():
    cfg = _config()
    rec = _recorder(cfg)
    managers = {s.key: rec._make_receiver_manager(s) for s in cfg.sources}
    assert set(managers) == {RADIOD, KIWI}
    assert type(managers[KIWI]) is KiwiSourceManager
    assert type(managers[RADIOD]) is ReceiverManager
    assert managers[KIWI].source_key == KIWI


def test_kiwi_band_uses_wallclock_correlation(caplog, t6_authority):
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    channels = []
    mgr = _kiwi_manager(rec, kiwi_src, channels=channels)
    try:
        with caplog.at_level(logging.INFO):
            assert mgr.connect()
            mgr.start_streams()
            _feed(channels[0], 2.0)
        rec_kiwi = rec.band_recorders[(KIWI, channels[0].freq_hz)]
        strat = rec_kiwi.sync_strategy
        assert strat.channel_info is None
        assert strat.authority_reader is None
        assert strat.correlation_source == "wallclock_fallback"
        assert any("correlated via wall clock" in r.getMessage()
                   for r in caplog.records)
    finally:
        mgr.shutdown()


def test_kiwi_rows_tier_is_not_injected(t6_authority, tmp_path):
    """With hf-timestd publishing T6 for the radiod, a Kiwi band still
    reports the standalone (no-authority) label everywhere a tier could
    surface: its anchor, the instance's applied block, the tier alarm and
    the WAV sidecar.  (Spot and noise rows carry no tier field; the
    sidecar and the status blocks are where a tier is stated.)"""
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    channels = []
    mgr = _kiwi_manager(rec, kiwi_src, channels=channels)
    try:
        mgr.connect()
        mgr.start_streams()
        _feed(channels[0], 2.0)
        rec_kiwi = rec.band_recorders[(KIWI, channels[0].freq_hz)]
        anchor = rec_kiwi.sync_strategy.anchor
        assert anchor.source == "wallclock_fallback"
        assert anchor.offset_ns is None
        assert anchor.timing_authority_applied() is None
        assert rec._applied_state() is None
        stats = rec_kiwi.get_stats()
        assert stats["timing_tier_active"] is None
        assert stats["timing_tier_best"] is None
    finally:
        mgr.shutdown()

    # Contrast: a radiod band built by the same recorder does read the
    # authority (so the Kiwi result above is the branch, not the fixture).
    from wspr_recorder.receiver_manager import ChannelState
    rec._build_sink(1234, ChannelState(frequency_hz=F20, band_name="20"),
                    source_key=RADIOD)
    assert rec.band_recorders[(RADIOD, 1234)].sync_strategy \
        .authority_reader is not None

    # WAV sidecar: the Kiwi slot names no hf-timestd tier and no radiod.
    writer = WavWriter(output_dir=tmp_path, sample_rate=12000,
                       sample_format="float32",
                       timing_service=rec.timing_service,
                       host_clock_only=rec._is_kiwi_source)

    def sidecar(rx_source):
        req = DecodeRequest(
            frequency_hz=F20, band_name="20",
            modes=[DecodeMode.W2], period_seconds=120,
            start_wallclock=datetime(2026, 11, 22, 12, 0,
                                     tzinfo=timezone.utc),
            start_rtp_timestamp=0, end_rtp_timestamp=1_440_000,
            samples=np.zeros(1_440_000, np.float32), gaps=[],
            rx_source=rx_source)
        wav = writer.write_period(req)
        return json.loads(wav.with_suffix(".json").read_text())["timing"]

    kiwi_timing = sidecar(KIWI)
    assert "T6" not in json.dumps(kiwi_timing)
    assert not kiwi_timing["timing_source"].startswith("HF_TIMESTD")
    assert kiwi_timing["gpsdo_source"] is None
    assert kiwi_timing["gpsdo_locked"] is False
    assert kiwi_timing["timing_authority"]["source"] == "standalone-fallback"
    radiod_timing = sidecar(RADIOD)
    assert radiod_timing["timing_source"] == "HF_TIMESTD_T6"


# --- carried-forward constraints ------------------------------------------

def test_stream_restored_log_names_source_outage_for_kiwi(caplog, t6_authority):
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    channels = []
    mgr = _kiwi_manager(rec, kiwi_src, channels=channels)
    try:
        mgr.connect()
        mgr.start_streams()
        ch = channels[0]
        _feed(ch, 1.0)
        with caplog.at_level(logging.WARNING):
            ch.on_state("dropped", "connection closed")
            mgr._open(mgr._bands[ch.freq_hz])
        msgs = [r.getMessage() for r in caplog.records]
        assert any("Stream restored after source outage" in m for m in msgs)
        assert not any("radiod outage" in m for m in msgs)
    finally:
        mgr.shutdown()


def test_stream_restored_none_keeps_host_clock_strategy(t6_authority):
    """on_stream_restored(None) (a Kiwi reconnect) resets the recorder and
    re-correlates on the host clock; set_channel_info(None) is harmless."""
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    channels = []
    mgr = _kiwi_manager(rec, kiwi_src, channels=channels)
    try:
        mgr.connect()
        mgr.start_streams()
        ch = channels[0]
        _feed(ch, 1.0)
        rec_kiwi = rec.band_recorders[(KIWI, ch.freq_hz)]
        with mock.patch("os._exit", side_effect=AssertionError("os._exit")):
            ch.on_state("dropped", "socket closed")     # outage
            mgr._open(mgr._bands[ch.freq_hz])            # next-cycle retry
            new_ch = channels[-1]
            assert new_ch is not ch
            assert mgr._bands[ch.freq_hz].channel_state.restore_count == 1
            _feed(new_ch, 1.0)
        strat = rec_kiwi.sync_strategy
        assert strat.channel_info is None
        assert strat.correlation_source == "wallclock_fallback"
    finally:
        mgr.shutdown()


def _health_recorder(managers):
    rec = object.__new__(WsprRecorder)
    rec._running = True
    rec.STARTUP_GRACE_PERIOD = 0
    rec.HEALTH_CHECK_INTERVAL = 0
    rec.receiver_managers = managers
    rec.cycle_batcher = None
    return rec


def _run_health(rec, seconds=0.3):
    async def go():
        t = asyncio.create_task(rec._health_check_loop())
        await asyncio.sleep(seconds)
        rec._running = False
        await asyncio.wait_for(t, 5)

    asyncio.run(go())


RADIOD_ONLY = ("reprovision_stale", "full_reset", "diagnose_radiod_side")


def _spy_radiod_only(mgr):
    for name in RADIOD_ONLY:
        setattr(mgr, name, MagicMock(name=name))


@pytest.mark.parametrize("behaviour", ["refuse", "badp"])
def test_health_tick_leaves_connected_kiwi_alone(behaviour, caplog):
    """Every channel refused or auth_failed: check_health() is False while
    state.connected is True.  The health loop must not shutdown()+connect()
    (off-cadence channel grabs, a resent bad password) and must never reach
    the radiod-only ladder."""
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    mgr = _kiwi_manager(rec, kiwi_src, behaviour=behaviour)
    assert mgr.connect()
    assert mgr.state.connected and not mgr.check_health()
    _spy_radiod_only(mgr)
    mgr.shutdown = MagicMock()
    mgr.connect = MagicMock()
    rec = _health_recorder({KIWI: mgr})
    with caplog.at_level(logging.WARNING):
        _run_health(rec)
    # Nothing radiod-flavoured is said about it either (pre-wiring, the
    # unproducible bands fell into the "above the radiod Nyquist" branch).
    assert not [r for r in caplog.records
                if "Channel health" in r.getMessage()]
    mgr.shutdown.assert_not_called()
    mgr.connect.assert_not_called()
    for name in RADIOD_ONLY:
        getattr(mgr, name).assert_not_called()


def test_health_tick_mixed_sources_radiod_ladder_unchanged():
    """A degraded radiod beside a Kiwi still gets its ladder; the Kiwi gets
    none of it."""
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    kiwi = _kiwi_manager(rec, kiwi_src, behaviour="refuse")
    assert kiwi.connect()
    _spy_radiod_only(kiwi)
    radiod = ReceiverManager(cfg, lambda *a: None,
                             status_address="bee1-hf-status.local",
                             port=5004, source_key=RADIOD)
    radiod.state.channels[1] = SimpleNamespace(
        frequency_hz=F20, band_name="20", is_active=False)
    radiod.state.channels[2] = SimpleNamespace(
        frequency_hz=F30, band_name="30", is_active=True)
    radiod.reprovision_stale = MagicMock()
    radiod.full_reset = MagicMock()
    rec = _health_recorder({RADIOD: radiod, KIWI: kiwi})
    _run_health(rec)
    assert radiod.reprovision_stale.called or radiod.full_reset.called
    for name in RADIOD_ONLY:
        getattr(kiwi, name).assert_not_called()


def test_health_tick_reconnects_kiwi_that_never_answered():
    """A Kiwi unreachable at startup beside a connected radiod was never
    started; the health check retries its connect() and starts it."""
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    state = {"b": "unreachable"}
    mgr = _kiwi_manager(rec, kiwi_src, behaviour=lambda f: state["b"])
    assert not mgr.connect()
    _spy_radiod_only(mgr)
    state["b"] = "connect"
    rec = _health_recorder({KIWI: mgr})
    try:
        _run_health(rec, seconds=0.5)
        assert mgr.state.connected
        assert mgr.channels_obtained() == 2
        assert mgr._streaming
        for name in RADIOD_ONLY:
            getattr(mgr, name).assert_not_called()
    finally:
        mgr.shutdown()


def test_watchdog_exempts_kiwi_only_station_without_channels():
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    mgr = _kiwi_manager(rec, kiwi_src, behaviour="refuse",
                        start_supervisor=True)
    mgr.connect()
    mgr.start_streams()
    assert rec._kiwi_sources_idle()
    # With any radiod source the radiod ingestion watchdog stays in force.
    rec.receiver_managers[RADIOD] = MagicMock(spec=ReceiverManager)
    assert not rec._kiwi_sources_idle()
    del rec.receiver_managers[RADIOD]
    # A Kiwi holding a channel is judged by sample progress as before.
    mgr.shutdown()
    mgr2 = _kiwi_manager(rec, kiwi_src, behaviour="connect")
    mgr2.connect()
    try:
        assert not rec._kiwi_sources_idle()
    finally:
        mgr2.shutdown()


def test_watchdog_loop_pings_while_kiwi_waits(monkeypatch):
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    mgr = _kiwi_manager(rec, kiwi_src, behaviour="badp",
                        start_supervisor=True)
    mgr.connect()
    mgr.start_streams()
    rec._running = True
    sent = []
    rec._sd_notify = sent.append
    rec._total_samples_received = lambda: 0
    monkeypatch.setenv("WATCHDOG_USEC", "2000000")
    import wspr_recorder.__main__ as m
    monkeypatch.setattr(m, "_ProgressGate",
                        lambda **kw: SimpleNamespace(
                            update=lambda p, now: False))

    async def go():
        t = asyncio.create_task(rec._watchdog_loop())
        await asyncio.sleep(0.05)
        rec._running = False
        t.cancel()

    try:
        asyncio.run(go())
    finally:
        mgr.shutdown()
    assert b"WATCHDOG=1" in sent


def test_watchdog_exemption_needs_a_live_kiwi_supervisor():
    """The exemption trusts the manager's supervisor to retry; with that
    thread dead nothing would, so a channel-less Kiwi-only station must
    stop feeding the watchdog and let systemd restart it."""
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    mgr = _kiwi_manager(rec, kiwi_src, behaviour="refuse",
                        start_supervisor=True)
    try:
        assert mgr.connect()
        mgr.start_streams()
        assert rec._kiwi_sources_idle()
        mgr.stop_streams()                     # supervisor thread exits
        assert not mgr.supervisor_alive()
        assert not rec._kiwi_sources_idle()
    finally:
        mgr.shutdown()


def test_watchdog_exemption_for_never_answered_kiwi_without_supervisor():
    """A Kiwi that never answered has no supervisor yet; the health loop
    retries its connect() (test_health_tick_reconnects_kiwi_that_never_
    answered), so it still counts as idle, not wedged."""
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    mgr = _kiwi_manager(rec, kiwi_src, behaviour="unreachable",
                        start_supervisor=True)
    try:
        assert not mgr.connect()
        assert not mgr.supervisor_alive()
        assert rec._kiwi_sources_idle()
    finally:
        mgr.shutdown()


def test_watchdog_exemption_survives_concurrent_manager_changes():
    """receiver_managers changing under the check (shutdown/rebuild on
    another thread) reads as not idle, never an exception."""
    cfg = _config()
    rec = _recorder(cfg)

    class Exploding(dict):
        def values(self):
            raise RuntimeError("dictionary changed size during iteration")

    rec.receiver_managers = Exploding()
    assert rec._kiwi_sources_idle() is False


def test_custom_key_kiwi_source_gets_host_clock_sidecar(t6_authority,
                                                        tmp_path):
    """A kiwisdr [[source]] with a custom key= is still timed by the host
    clock: the sidecar is chosen by the source's kind, not its key text."""
    cfg = _config(TOML.replace('kiwisdr = "10.22.23.70:8073"',
                               'kiwisdr = "10.22.23.70:8073"\n'
                               'key = "lab-kiwi"'))
    rec = _recorder(cfg)
    assert any(s.key == "lab-kiwi" and s.kind == "kiwisdr"
               for s in cfg.sources)
    writer = WavWriter(output_dir=tmp_path, sample_rate=12000,
                       sample_format="float32",
                       timing_service=rec.timing_service,
                       host_clock_only=rec._is_kiwi_source)

    def sidecar(rx_source):
        req = DecodeRequest(
            frequency_hz=F20, band_name="20",
            modes=[DecodeMode.W2], period_seconds=120,
            start_wallclock=datetime(2026, 11, 22, 12, 0,
                                     tzinfo=timezone.utc),
            start_rtp_timestamp=0, end_rtp_timestamp=1_440_000,
            samples=np.zeros(1_440_000, np.float32), gaps=[],
            rx_source=rx_source)
        wav = writer.write_period(req)
        return json.loads(wav.with_suffix(".json").read_text())["timing"]

    kiwi_timing = sidecar("lab-kiwi")
    assert "T6" not in json.dumps(kiwi_timing)
    assert kiwi_timing["timing_authority"]["source"] == "standalone-fallback"
    assert sidecar(RADIOD)["timing_source"] == "HF_TIMESTD_T6"


def test_recorder_wires_wav_writer_host_clock_to_source_kind():
    """__main__ hands WavWriter the kind-based predicate it uses itself."""
    import inspect
    from wspr_recorder import __main__ as m
    src = inspect.getsource(m.WsprRecorder)
    assert "host_clock_only=self._is_kiwi_source" in src


def test_kiwi_overloads_never_poll_the_radiod():
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    kiwi = _kiwi_manager(rec, kiwi_src)
    kiwi.read_frontend_ad_over = MagicMock(return_value=7)
    kiwi.read_channel_filter_drops = MagicMock(return_value=None)
    radiod = MagicMock()
    radiod._status_address = "bee1-hf-status.local"
    rec.receiver_managers = {RADIOD: radiod, KIWI: kiwi}
    rec.band_recorders = {
        (KIWI, F20): SimpleNamespace(frequency_hz=F20),
        (RADIOD, 99): SimpleNamespace(frequency_hz=F20),
    }
    rec._last_ad_over = {}
    rec._last_filter_drops = {}
    req = SimpleNamespace(rx_source=KIWI, frequency_hz=F20, band_name="20")
    rec._read_cycle_overloads(req, "bee1-hf-status.local")
    rec._read_cycle_filter_drops(req, "bee1-hf-status.local")
    kiwi.read_frontend_ad_over.assert_called_once_with(F20)
    radiod.read_frontend_ad_over.assert_not_called()
    radiod.read_channel_filter_drops.assert_not_called()
    assert (KIWI, "20") in rec._last_ad_over


def test_kiwi_status_reports_gps_fix_field():
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    mgr = _kiwi_manager(rec, kiwi_src)
    try:
        mgr.connect()
        st = mgr.get_status()
        assert st["kiwi_gps_fix"] is False
    finally:
        mgr.shutdown()


# --- a drop just after the cycle boundary keeps the completed cycle --------

class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _drop_after_boundary(monkeypatch, past_boundary_s, in_rate=12000.0):
    """Connect at :32 of an odd minute (like the lab run at 21:09:32Z),
    stream through the whole next even-minute cycle plus
    ``past_boundary_s``, then have the Kiwi close the socket and reconnect.
    Returns the start times of every slot the recorder handed on."""
    import wspr_recorder.band_recorder as br

    cycle_start = 1_795_000_000 - 1_795_000_000 % 120 + 120   # an even minute
    clock = _Clock(cycle_start - 28.0)                        # hh:m9:32

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock.t, tz=tz)

    monkeypatch.setattr(br, "datetime", FakeDatetime)
    cfg = _config()
    rec = _recorder(cfg)
    kiwi_src = next(s for s in cfg.sources if s.kind == "kiwisdr")
    channels = []
    mgr = KiwiSourceManager(
        rec.config,
        lambda ssrc, st, sk=kiwi_src.key: rec._build_sink(
            ssrc, st, source_key=sk),
        kiwi_src, None, clock=clock,
        channel_factory=lambda *a, **kw: channels.append(
            FakeChannel(*a, **kw)) or channels[-1],
        start_supervisor=False)
    try:
        mgr.connect()
        mgr.start_streams()
        ch = next(c for c in channels if c.freq_hz == F20)
        ch.sample_rate = in_rate
        frame = 1200
        n_frames = int(round((28.0 + 120.0 + past_boundary_s) * 12000 / frame))
        for _ in range(n_frames):
            clock.t += frame / 12000
            ch.on_audio(np.zeros(frame, np.float32), 0.0)
        with mock.patch("os._exit", side_effect=AssertionError("os._exit")):
            ch.on_state("dropped", "socket closed")
            clock.t += 1.0
            mgr._open(mgr._bands[F20])
            for _ in range(10):
                clock.t += frame / 12000
                channels[-1].on_audio(np.zeros(frame, np.float32), 0.0)
        return cycle_start, [
            c.args[1].start_wallclock.timestamp()
            for c in rec.executor.submit.call_args_list
            if c.args[1].band_name == "20"]
    finally:
        mgr.shutdown()


@pytest.mark.parametrize("past_boundary_s", [4.0, 8.0, 0.0])
def test_kiwi_drop_after_boundary_keeps_completed_cycle(monkeypatch,
                                                        past_boundary_s):
    cycle_start, slots = _drop_after_boundary(monkeypatch, past_boundary_s)
    assert slots == [cycle_start]


def test_kiwi_drop_after_boundary_keeps_cycle_with_crystal_rate(monkeypatch):
    """A crystal-clocked Kiwi (~12001.1 Hz) is resampled; the completed
    cycle must still be handed on before the drop."""
    cycle_start, slots = _drop_after_boundary(monkeypatch, 4.0,
                                              in_rate=12001.1)
    assert slots == [cycle_start]
