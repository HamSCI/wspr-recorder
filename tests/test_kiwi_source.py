"""KiwiSourceManager tests: fake channels, fake time, no sockets or sleeps."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import numpy as np
import pytest

from wspr_recorder.config import SourceConfig
from wspr_recorder.kiwi_source import KiwiSourceManager, STALL_TIMEOUT_S
from wspr_recorder.receiver_manager import ChannelSink

F20, F30, F40 = 14095600, 10138700, 7038600
HOST = "10.22.23.70"


class FakeClock:
    def __init__(self, t: float = 1_000_020.0):  # 20 s into a 120 s cycle
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeChannel:
    """Answers start() synchronously according to ``behaviour[freq]``."""

    def __init__(self, behaviour, rate, host, port, freq_hz, *, password, user,
                 on_audio, on_state):
        self.host, self.port, self.freq_hz = host, port, freq_hz
        self.password, self.user = password, user
        self.on_audio, self.on_state = on_audio, on_state
        self._behaviour = behaviour
        self.sample_rate = rate.get(freq_hz, 12000.0)
        self.started = 0
        self.stopped = False
        self.connected = False   # streaming audio, as far as the test knows

    def start(self):
        assert self.started == 0, "KiwiChannel is single-use"
        self.started += 1
        b = self._behaviour.get(self.freq_hz, "connect")
        if b == "connect":
            self.connected = True
            self.on_state("connected", f"sample_rate={self.sample_rate}")
        elif b == "refuse":
            self.on_state("refused", "too_busy=1")
        elif b == "badp":
            self.on_state("auth_failed", "badp=1")
        elif b == "unreachable":
            self.on_state("dropped", "connection refused")

    def stop(self):
        self.stopped = True
        self.connected = False


_RIGS = []


@pytest.fixture(autouse=True)
def _shutdown_rigs():
    yield
    while _RIGS:
        _RIGS.pop().mgr.shutdown()


class Rig:
    def __init__(self, freqs, behaviour=None, rate=None, clock=None, **kw):
        _RIGS.append(self)
        self.events = []        # ("dropped"|"restored"|"samples", ssrc)
        self.behaviour = dict(behaviour or {})
        self.rate = dict(rate or {})
        self.clock = clock or FakeClock()
        self.channels = []
        self.received = {}      # ssrc -> list of (samples, gaps, total)
        self.qualities = {}
        self.dropped = []
        self.restored = []
        self.producible = []
        cfg = SimpleNamespace(frequencies=list(freqs))
        src = SourceConfig(key=f"kiwisdr:{HOST}:8073", status_address="",
                           kind="kiwisdr", host=HOST, http_port=8073,
                           channels=8)
        self.mgr = KiwiSourceManager(
            cfg, self.sink_factory, src, self.on_producible,
            clock=self.clock, channel_factory=self.make_channel,
            start_supervisor=False, **kw)

    def make_channel(self, *a, **kw):
        ch = FakeChannel(self.behaviour, self.rate, *a, **kw)
        self.channels.append(ch)
        return ch

    def sink_factory(self, ssrc, state):
        self.received[ssrc] = []

        def on_samples(samples, quality):
            self.qualities[ssrc] = quality
            self.events.append(("samples", ssrc))
            self.received[ssrc].append(
                (np.asarray(samples), list(quality.batch_gaps),
                 quality.total_samples_delivered))

        return ChannelSink(
            on_samples=on_samples,
            on_stream_dropped=lambda reason: (
                self.dropped.append(ssrc),
                self.events.append(("dropped", ssrc))),
            on_stream_restored=lambda info: (
                self.restored.append(ssrc),
                self.events.append(("restored", ssrc))))

    def on_producible(self, source_key, band, producible):
        self.producible.append((source_key, band, producible))

    def live(self, freq):
        """The most recent channel for freq."""
        return [c for c in self.channels if c.freq_hz == freq][-1]

    def starts(self, freq):
        return sum(1 for c in self.channels if c.freq_hz == freq)

    def feed(self, freq, seconds, frame=512):
        """Deliver ``seconds`` of audio at the channel's rate, advancing time."""
        ch = self.live(freq)
        n = int(round(seconds * ch.sample_rate))
        for i in range(0, n, frame):
            k = min(frame, n - i)
            self.clock.t += k / ch.sample_rate
            ch.on_audio(np.full(k, 0.25, np.float32), self.clock.t)
            self.mgr.tick()

    def advance(self, seconds, step=1.0, audio=True):
        """Advance fake time; connected channels keep streaming if ``audio``."""
        end = self.clock.t + seconds
        while self.clock.t < end:
            dt = min(step, end - self.clock.t)
            self.clock.t += dt
            if audio:
                for ch in [c for c in self.channels if c.connected]:
                    ch.on_audio(np.zeros(int(round(dt * ch.sample_rate)),
                                         np.float32), self.clock.t)
            self.mgr.tick()


def test_one_channel_per_band_and_status_counts():
    rig = Rig([F20, F30, F40], behaviour={F30: "refuse"})
    assert rig.mgr.connect() is True
    assert rig.mgr.state.connected
    assert sorted(c.freq_hz for c in rig.channels) == sorted([F20, F30, F40])
    assert all(c.host == HOST and c.port == 8073 for c in rig.channels)
    assert all(c.user.startswith("wspr-recorder_v") for c in rig.channels)
    st = rig.mgr.get_status()
    assert st["channels_obtained"] == 2
    assert st["channels_wanted"] == 3
    # Synthetic SSRC per band = its frequency.
    assert set(rig.received) == {F20, F30, F40}
    assert rig.mgr.read_frontend_ad_over(F20) is None


def test_refused_band_not_producible_and_retried_next_cycle():
    rig = Rig([F20, F30], behaviour={F30: "refuse"})
    rig.mgr.connect()
    rig.mgr.start_streams()
    assert (rig.mgr.source_key, "30", False) in rig.producible
    assert rig.starts(F30) == 1
    # Still refused: exactly one retry per 120 s cycle, never a tight loop.
    rig.advance(600)
    assert rig.starts(F30) == 1 + 5
    # Channel frees up: next cycle's retry gets it; band producible again.
    rig.behaviour[F30] = "connect"
    rig.advance(120)
    assert rig.starts(F30) == 7
    assert rig.producible[-1] == (rig.mgr.source_key, "30", True)
    assert rig.mgr.get_status()["channels_obtained"] == 2
    rig.advance(600)
    assert rig.starts(F30) == 7   # connected channels are left alone
    assert rig.starts(F20) == 1


def test_auth_failed_not_retried(caplog):
    rig = Rig([F20], behaviour={F20: "badp"})
    with caplog.at_level(logging.ERROR, logger="wspr_recorder.kiwi_source"):
        rig.mgr.connect()
        rig.mgr.start_streams()
        rig.advance(600)
    assert rig.producible == [(rig.mgr.source_key, "20", False)]
    assert rig.starts(F20) == 1
    errors = [r for r in caplog.records if "password" in r.getMessage()]
    assert len(errors) == 1


def test_samples_reach_band_recorder_at_12000():
    rig = Rig([F20], rate={F20: 12001.135})
    rig.mgr.connect()
    rig.mgr.start_streams()
    rig.feed(F20, 30.0)
    batches = rig.received[F20]
    total = sum(len(s) for s, _, _ in batches)
    assert abs(total - 12000 * 30) <= 64
    totals = [t for _, _, t in batches]
    assert totals == sorted(totals) and totals[-1] == total
    q = rig.qualities[F20]
    assert q.sample_rate == 12000 and q.first_rtp_timestamp == 0
    assert q.batch_gaps == []


def test_external_clock_kiwi_passes_through_and_follows_rate_change():
    rig = Rig([F20], rate={F20: 12000.0})
    rig.mgr.connect()
    rig.mgr.start_streams()
    rig.feed(F20, 1.0, frame=500)
    assert [len(s) for s, _, _ in rig.received[F20]][:3] == [500, 500, 500]
    # The Kiwi now reports a crystal rate: the resampler follows it.
    rig.live(F20).sample_rate = 12001.135
    rig.received[F20].clear()
    rig.feed(F20, 10.0)
    total = sum(len(s) for s, _, _ in rig.received[F20])
    assert abs(total - 120000) <= 64


def test_audio_before_start_streams_is_not_delivered():
    rig = Rig([F20])
    rig.mgr.connect()
    rig.feed(F20, 1.0)
    assert rig.received[F20] == []


def test_drop_records_gap_and_reconnects_at_cycle_boundary():
    clock = FakeClock(1_000_080.0)          # cycle starts at 999_960 + 120
    rig = Rig([F20], clock=clock)
    rig.mgr.connect()
    rig.mgr.start_streams()
    rig.feed(F20, 5.0)                        # t ≈ 1_000_085
    last_audio = clock.t
    rig.live(F20).connected = False
    rig.live(F20).on_state("dropped", "1006 reset")
    assert rig.dropped == [F20]
    assert rig.mgr.get_status()["channels_obtained"] == 0
    # No reconnect before the next even-minute boundary (1_000_200).
    rig.advance(1_000_199.0 - clock.t, audio=False)
    assert rig.starts(F20) == 1
    rig.advance(2.0, audio=False)
    assert rig.starts(F20) == 2
    assert rig.restored == [F20]              # recorder resets: no stitching
    # First audio after reconnect carries the gap of the missing length.
    n_before = len(rig.received[F20])
    rig.feed(F20, 1.0)
    _, gaps, _ = rig.received[F20][n_before]
    assert len(gaps) == 1
    expected = round((clock.t - (1.0 - 512 / 12000) - last_audio) * 12000)
    assert abs(gaps[0].duration_samples - expected) <= 512 + 1
    assert gaps[0].duration_samples > 115 * 12000
    # Only the first batch after reconnect carries it.
    assert all(g == [] for _, g, _ in rig.received[F20][n_before + 1:])


def test_stalled_channel_is_treated_as_dropped():
    clock = FakeClock(1_000_080.0)
    rig = Rig([F20], clock=clock)
    rig.mgr.connect()
    rig.mgr.start_streams()
    rig.feed(F20, 2.0)
    ch = rig.live(F20)
    rig.advance(STALL_TIMEOUT_S - 1.0, audio=False)
    assert rig.dropped == [] and not ch.stopped
    rig.advance(2.0, audio=False)
    assert rig.dropped == [F20] and ch.stopped
    # Late audio from the abandoned channel is ignored.
    ch.on_audio(np.zeros(512, np.float32), clock.t)
    n = len(rig.received[F20])
    rig.advance(1_000_201.0 - clock.t, audio=False)
    assert rig.starts(F20) == 2
    assert len(rig.received[F20]) == n
    rig.feed(F20, 0.5)
    _, gaps, _ = rig.received[F20][n]
    assert len(gaps) == 1 and gaps[0].duration_samples > 100 * 12000


def test_cycle_log_line_n_of_m(caplog):
    rig = Rig([F20, F30, F40], behaviour={F40: "refuse"})
    with caplog.at_level(logging.INFO, logger="wspr_recorder.kiwi_source"):
        rig.mgr.connect()
        rig.mgr.start_streams()
        rig.advance(240)
    msgs = [r.getMessage() for r in caplog.records]
    lines = [m for m in msgs if "channels obtained" in m]
    assert f"kiwisdr:{HOST}:8073: 2 of 3 channels obtained" in lines
    assert len(lines) == 3            # connect + one per cycle boundary


def test_shutdown_stops_channels_and_connect_again_rebuilds():
    rig = Rig([F20])
    rig.mgr.connect()
    rig.mgr.start_streams()
    first = rig.live(F20)
    rig.mgr.shutdown()
    assert first.stopped and not rig.mgr.state.connected
    assert rig.mgr.connect() is True
    assert rig.live(F20) is not first


def test_unreachable_kiwi_connect_returns_false():
    rig = Rig([F20, F30], behaviour={F20: "unreachable", F30: "unreachable"})
    assert rig.mgr.connect() is False
    assert not rig.mgr.state.connected
    assert rig.mgr.check_health() is False


def test_bad_rate_mid_stream_is_an_outage():
    clock = FakeClock(1_000_080.0)
    rig = Rig([F20], clock=clock)
    rig.mgr.connect()
    rig.mgr.start_streams()
    rig.feed(F20, 2.0)
    ch = rig.live(F20)
    ch.connected = False
    ch.on_state("bad_rate", "20250.0")
    assert rig.dropped == [F20]
    assert rig.producible[-1] == (rig.mgr.source_key, "20", False)
    assert rig.mgr.get_status()["channels"][F20]["state"] == "bad_rate"
    mark = len(rig.events)
    rig.advance(1_000_201.0 - clock.t, audio=False)
    assert rig.starts(F20) == 2
    rig.feed(F20, 0.5)
    # The recorder is reset before any post-reconnect audio reaches it.
    after = rig.events[mark:]
    assert after[0] == ("restored", F20)
    assert after[1:] and all(e == ("samples", F20) for e in after[1:])
    _, gaps, _ = [b for b in rig.received[F20] if b[1]][0]
    assert len(gaps) == 1 and gaps[0].duration_samples > 100 * 12000


def test_refused_mid_stream_is_an_outage():
    clock = FakeClock(1_000_080.0)
    rig = Rig([F20], clock=clock)
    rig.mgr.connect()
    rig.mgr.start_streams()
    rig.feed(F20, 2.0)
    rig.live(F20).connected = False
    rig.live(F20).on_state("refused", "down")
    assert rig.dropped == [F20]
    assert rig.mgr.get_status()["channels"][F20]["state"] == "refused"
    rig.advance(1_000_201.0 - clock.t, audio=False)
    assert rig.restored == [F20]


def test_connecting_channel_that_never_answers_is_retried():
    rig = Rig([F20, F30], behaviour={F30: "silent"}, connect_timeout_s=0.01)
    assert rig.mgr.connect() is True
    rig.mgr.start_streams()
    silent = rig.live(F30)
    assert rig.mgr.get_status()["channels"][F30]["state"] == "connecting"
    rig.advance(2.0)
    assert silent.stopped
    st = rig.mgr.get_status()["channels"][F30]
    assert st["state"] == "unreachable"
    assert (rig.mgr.source_key, "30", False) in rig.producible
    # Retried once at the next cycle boundary (t=1_000_080), not before.
    assert rig.starts(F30) == 1
    rig.advance(1_000_081.0 - rig.clock.t)
    assert rig.starts(F30) == 2


def test_no_channel_opened_after_shutdown():
    rig = Rig([F20, F30], behaviour={F30: "refuse"})
    rig.mgr.connect()
    band = rig.mgr._bands[F30]          # an in-flight tick holds this
    rig.mgr.shutdown()
    n = len(rig.channels)
    rig.mgr._open(band)
    assert len(rig.channels) == n
