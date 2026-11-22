"""KiwiSourceManager: one KiwiSDR feeding BandRecorders, one channel per band.

The ReceiverManager counterpart for a ``kind = "kiwisdr"`` ``[[source]]``.
Each configured band gets its own Kiwi WebSocket audio channel
(:class:`KiwiChannel`); its ~12000 Hz audio is resampled to exactly 12000 Hz
(:class:`StreamResampler`, a passthrough for an external-clock Kiwi) and
handed to the band's sink with a :class:`KiwiStreamQuality`, whose sample
counter stands in for the RTP timestamp.

Channel policy (spec §7b.6, never fight web listeners for channels):

* a refused (``too_busy``) or unreachable channel is retried once per
  2-minute WSPR cycle, at the even-minute boundary, never in a tight loop;
* a rejected password (``badp``) is a configuration fault: logged once,
  never retried;
* a channel that drops mid-cycle (or goes silent for ``stall_timeout_s``)
  is reported to the sink as dropped and reconnected at the next even
  minute.  On reconnect the sink's ``on_stream_restored`` resets the
  recorder so no stale samples are stitched across the outage, and the
  first batch afterwards carries one gap of the missing wall-clock length.

Every cycle the manager logs ``kiwisdr:<host>:<port>: N of M channels
obtained``; ``get_status()`` carries the same two numbers.

KiwiChannel objects are single-use: every connection attempt builds a new
one through ``channel_factory``.  ``clock`` (wall-clock seconds) and
``channel_factory`` are injectable so tests run on fake time.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np

from . import __version__
from .config import Config, SourceConfig, freq_to_band_name
from .kiwi_channel import KiwiChannel
from .kiwi_resample import StreamResampler
from .receiver_manager import ChannelSink, ChannelState

logger = logging.getLogger(__name__)

OUT_RATE = 12000
CYCLE_S = 120
# A connected channel with no audio for this long is treated as dropped
# (KiwiChannel has no watchdog of its own; a Kiwi frame is ~43 ms).
STALL_TIMEOUT_S = 10.0
# How long connect() waits for every channel's first answer, and how long
# after a cycle boundary the "N of M" line waits for retries to settle.
CONNECT_TIMEOUT_S = 10.0
SUPERVISOR_PERIOD_S = 1.0

USER_AGENT = f"wspr-recorder_v{__version__}"

# Band states.
_CONNECTING = "connecting"
_CONNECTED = "connected"
_REFUSED = "refused"          # too_busy / down / redirect: retry next cycle
_UNREACHABLE = "unreachable"  # never connected: retry next cycle
_BAD_RATE = "bad_rate"        # rate outside 11990..12010: retry next cycle
_DROPPED = "dropped"          # lost after connecting: reconnect next cycle
_AUTH_FAILED = "auth_failed"  # wrong password: never retried
_STOPPED = "stopped"

_RETRY_STATES = (_REFUSED, _UNREACHABLE, _BAD_RATE, _DROPPED)


@dataclass
class KiwiGap:
    """A stretch of audio the Kiwi never delivered (BandRecorder reads
    ``duration_samples``; it caps the value itself)."""
    duration_samples: int
    start_time: float = 0.0


@dataclass
class KiwiStreamQuality:
    """The StreamQuality attributes BandRecorder.on_samples reads.

    ``first_rtp_timestamp + total_samples_delivered`` is the synthetic RTP
    timestamp: a count of 12000 Hz samples delivered, monotonic for the
    life of the band.  ``batch_gaps`` holds only the current batch's gaps.
    """
    first_rtp_timestamp: int = 0
    total_samples_delivered: int = 0
    batch_gaps: List[KiwiGap] = field(default_factory=list)
    sample_rate: int = OUT_RATE
    total_samples_expected: int = 0
    total_gaps_filled: int = 0
    rtp_packets_received: int = 0
    rtp_packets_expected: int = 0
    rtp_packets_lost: int = 0
    rtp_packets_late: int = 0
    rtp_packets_duplicate: int = 0
    rtp_packets_resequenced: int = 0


@dataclass
class _Band:
    freq_hz: int
    band_name: str
    ssrc: int
    channel_state: ChannelState
    sink: ChannelSink
    quality: KiwiStreamQuality = field(default_factory=KiwiStreamQuality)
    lock: threading.RLock = field(default_factory=threading.RLock)
    status: str = _STOPPED
    channel: Optional[KiwiChannel] = None
    generation: int = 0
    resampler: Optional[StreamResampler] = None
    last_audio_t: Optional[float] = None
    gap_from: Optional[float] = None      # last audio before an outage
    awaiting_restore: bool = False        # an outage happened; reset on reconnect
    retry_cycle: Optional[int] = None
    producible: Optional[bool] = None
    detail: str = ""


class _KiwiManagerState:
    def __init__(self, key: str, host: str, port: int):
        self.connected = False
        self.radiod_address = key
        self.port = port
        self.host = host
        self.channels: Dict[int, ChannelState] = {}


class KiwiSourceManager:
    """Opens one Kiwi channel per configured band and feeds its sink."""

    quiet_failures = False

    def __init__(
        self,
        config: Config,
        sink_factory: Callable[[int, ChannelState], ChannelSink],
        source: SourceConfig,
        on_band_producible: Optional[Callable[[str, str, bool], None]] = None,
        *,
        clock: Callable[[], float] = time.time,
        channel_factory: Callable[..., KiwiChannel] = KiwiChannel,
        stall_timeout_s: float = STALL_TIMEOUT_S,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
    ):
        self.config = config
        self.sink_factory = sink_factory
        self.source = source
        self.source_key = source.key
        self._on_band_producible = on_band_producible
        self._clock = clock
        self._channel_factory = channel_factory
        self._stall_timeout_s = stall_timeout_s
        self._connect_timeout_s = connect_timeout_s
        self.state = _KiwiManagerState(source.key, source.host,
                                       source.http_port)
        self._bands: Dict[int, _Band] = {}
        self._lifetime_entries: list = []   # radiod-only; kept for __main__
        self._streaming = False
        self._auth_logged = False
        self._cycle: Optional[int] = None
        self._log_pending_since: Optional[float] = None
        self._settled = threading.Condition()
        self._supervisor: Optional[threading.Thread] = None
        self._stop_supervisor = threading.Event()

    # ---- ReceiverManager surface ---------------------------------------

    def connect(self) -> bool:
        """Open a channel per band; True once the Kiwi has answered.

        Waits (up to ``connect_timeout_s``) for every channel's first
        answer.  Returns False only when no channel got any answer from the
        Kiwi (unreachable): the caller then shuts down and retries.  A Kiwi
        that answers but refuses every channel is connected; its bands are
        retried here, once per cycle.
        """
        now = self._clock()
        self._cycle = int(now // CYCLE_S)
        for freq_hz in self.config.frequencies:
            if freq_hz in self._bands:
                continue
            band_name = freq_to_band_name(freq_hz)
            cs = ChannelState(frequency_hz=freq_hz, band_name=band_name,
                              ssrc=freq_hz, created_at=now)
            self.state.channels[freq_hz] = cs
            self._bands[freq_hz] = _Band(
                freq_hz=freq_hz, band_name=band_name, ssrc=freq_hz,
                channel_state=cs, sink=self.sink_factory(freq_hz, cs))
        for band in self._bands.values():
            if band.status == _STOPPED:   # a repeated connect() opens nothing twice
                self._open(band)

        deadline = time.monotonic() + self._connect_timeout_s
        with self._settled:
            while any(b.status == _CONNECTING for b in self._bands.values()):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._settled.wait(left)

        answered = any(b.status in (_CONNECTED, _REFUSED, _AUTH_FAILED,
                                    _BAD_RATE)
                       for b in self._bands.values())
        self.state.connected = answered
        if answered:
            self.quiet_failures = False
            self._log_counts()
        else:
            (logger.debug if self.quiet_failures else logger.error)(
                "%s: KiwiSDR unreachable (%s)", self.source_key,
                "; ".join(f"{b.band_name}: {b.detail}"
                          for b in self._bands.values()))
        return answered

    def start_streams(self) -> None:
        """Begin delivering audio to the sinks and start the supervisor."""
        self._streaming = True
        if self._supervisor is None or not self._supervisor.is_alive():
            self._stop_supervisor.clear()
            self._supervisor = threading.Thread(
                target=self._supervise, name=f"kiwi-supervisor-{self.source_key}",
                daemon=True)
            self._supervisor.start()

    def stop_streams(self) -> None:
        self._streaming = False
        self._stop_supervisor.set()
        sup = self._supervisor
        if sup is not None and sup is not threading.current_thread():
            sup.join(timeout=5)
        self._supervisor = None

    def shutdown(self) -> None:
        self.stop_streams()
        for band in list(self._bands.values()):
            with band.lock:
                ch = band.channel
                band.channel = None
                band.generation += 1
                band.status = _STOPPED
            if ch is not None:
                ch.stop()
        self._bands.clear()
        self.state.channels.clear()
        self.state.connected = False

    def check_health(self) -> bool:
        now = self._clock()
        return any(
            b.status == _CONNECTED and b.last_audio_t is not None
            and now - b.last_audio_t < self._stall_timeout_s
            for b in self._bands.values())

    def channels_obtained(self) -> int:
        return sum(1 for b in self._bands.values() if b.status == _CONNECTED)

    def get_status(self) -> dict:
        now = self._clock()
        channels = {}
        active = 0
        for ssrc, b in self._bands.items():
            cs = b.channel_state
            is_active = (b.status == _CONNECTED and b.last_audio_t is not None
                         and now - b.last_audio_t < self._stall_timeout_s)
            active += is_active
            channels[ssrc] = {
                "frequency_hz": b.freq_hz,
                "band_name": b.band_name,
                "state": b.status,
                "detail": b.detail,
                "is_active": is_active,
                "samples_received": cs.samples_received,
                "drop_count": cs.drop_count,
                "restore_count": cs.restore_count,
                "kiwi_sample_rate": (b.resampler.in_rate
                                     if b.resampler is not None else None),
            }
        return {
            "kind": "kiwisdr",
            "connected": self.state.connected,
            "source_key": self.source_key,
            "host": self.source.host,
            "port": self.source.http_port,
            "channels_obtained": self.channels_obtained(),
            "channels_wanted": len(self.config.frequencies),
            "channel_count": len(self._bands),
            "active_channels": active,
            "channels": channels,
        }

    def record_samples(self, ssrc: int, count: int) -> None:
        cs = self.state.channels.get(ssrc)
        if cs is not None:
            cs.last_sample_time = self._clock()
            cs.samples_received += count

    def unproducible_freqs(self) -> set:
        return {f for f, b in self._bands.items() if b.producible is False}

    def read_frontend_ad_over(self, ssrc: int) -> Optional[int]:
        """Kiwi ADC overload count: unknown until Task 6 reads /status."""
        return None

    def read_channel_filter_drops(self, ssrc: int) -> Optional[int]:
        return None

    # ---- supervision ---------------------------------------------------

    def _supervise(self) -> None:
        while not self._stop_supervisor.wait(SUPERVISOR_PERIOD_S):
            try:
                self.tick()
            except Exception:
                logger.exception("%s: supervisor tick failed", self.source_key)

    def tick(self) -> None:
        """One supervision pass: stall watchdog, cycle-boundary retries and
        the per-cycle "N of M" line.  Called every second by the supervisor
        thread (and directly by tests on fake time)."""
        now = self._clock()
        cycle = int(now // CYCLE_S)

        if self._streaming:
            for band in list(self._bands.values()):
                self._check_stall(band, now)

        if self._cycle is None or cycle != self._cycle:
            self._cycle = cycle
            for band in list(self._bands.values()):
                with band.lock:
                    due = (band.status in _RETRY_STATES
                           and band.retry_cycle is not None
                           and band.retry_cycle <= cycle)
                if due:
                    self._open(band)
            self._log_pending_since = now

        if self._log_pending_since is not None:
            settling = any(b.status == _CONNECTING
                           for b in self._bands.values())
            if (not settling
                    or now - self._log_pending_since >= self._connect_timeout_s):
                self._log_counts()

    def _log_counts(self) -> None:
        self._log_pending_since = None
        logger.info("%s: %d of %d channels obtained", self.source_key,
                    self.channels_obtained(), len(self.config.frequencies))

    def _check_stall(self, band: _Band, now: float) -> None:
        with band.lock:
            if (band.status != _CONNECTED or band.last_audio_t is None
                    or now - band.last_audio_t < self._stall_timeout_s):
                return
            ch = band.channel
            silent = now - band.last_audio_t
            self._mark_dropped(band, f"no audio for {silent:.0f} s", now)
        if ch is not None:
            ch.stop()

    # ---- channel lifecycle ---------------------------------------------

    def _open(self, band: _Band) -> None:
        with band.lock:
            band.generation += 1
            gen = band.generation
            band.status = _CONNECTING
            band.retry_cycle = None
            ch = self._channel_factory(
                self.source.host, self.source.http_port, band.freq_hz,
                password=self.source.password, user=USER_AGENT,
                on_audio=lambda s, t: self._on_audio(band, gen, s),
                on_state=lambda st, d: self._on_state(band, gen, st, d))
            band.channel = ch
        ch.start()

    def _on_state(self, band: _Band, gen: int, state: str, detail: str) -> None:
        now = self._clock()
        with band.lock:
            if gen != band.generation:
                return
            band.detail = detail
            was = band.status
            if state == "connected":
                band.status = _CONNECTED
                band.last_audio_t = now
                band.resampler = StreamResampler(self._rate_of(band))
                logger.info("%s %s (%d Hz): channel obtained, %s",
                            self.source_key, band.band_name, band.freq_hz,
                            detail)
                if band.awaiting_restore:
                    band.awaiting_restore = False
                    band.sink.on_stream_restored(None)
                self._set_producible(band, True)
            elif state == "auth_failed":
                band.status = _AUTH_FAILED
                band.channel = None
                if not self._auth_logged:
                    self._auth_logged = True
                    logger.error(
                        "%s: the KiwiSDR rejected the password (%s); check "
                        "password in the [[source]] entry. Not retrying.",
                        self.source_key, detail)
                self._set_producible(band, False)
            elif state == "dropped" and was == _CONNECTED:
                self._mark_dropped(band, detail or "connection closed", now)
            else:
                # refused, bad_rate, or a drop before we ever connected.
                band.status = {"refused": _REFUSED,
                               "bad_rate": _BAD_RATE}.get(state, _UNREACHABLE)
                band.channel = None
                band.retry_cycle = int(now // CYCLE_S) + 1
                log = logger.error if state == "bad_rate" else logger.warning
                if state == "bad_rate":
                    msg = "Kiwi audio rate %s Hz is not ~12000 Hz; channel refused"
                elif state == "refused":
                    msg = "no channel (%s); retrying next cycle"
                else:
                    msg = "Kiwi not reachable (%s); retrying next cycle"
                log("%s %s: " + msg, self.source_key, band.band_name, detail)
                self._set_producible(band, False)
        with self._settled:
            self._settled.notify_all()

    def _mark_dropped(self, band: _Band, reason: str, now: float) -> None:
        """Caller holds band.lock."""
        band.generation += 1          # ignore anything the old channel sends
        band.channel = None
        band.status = _DROPPED
        band.retry_cycle = int(now // CYCLE_S) + 1
        if band.gap_from is None:
            band.gap_from = band.last_audio_t
        band.awaiting_restore = True
        logger.warning("%s %s: channel dropped (%s); reconnecting at the next "
                       "even minute", self.source_key, band.band_name, reason)
        band.sink.on_stream_dropped(reason)

    def _on_audio(self, band: _Band, gen: int, samples: np.ndarray) -> None:
        now = self._clock()
        with band.lock:
            if gen != band.generation or band.status != _CONNECTED:
                return
            band.last_audio_t = now
            if not self._streaming:
                return
            rs = band.resampler
            rate = self._rate_of(band)
            if rate != rs.in_rate:
                rs.set_in_rate(rate)
            gaps: List[KiwiGap] = []
            if band.gap_from is not None:
                missing_s = (now - band.gap_from) - len(samples) / rate
                gaps.append(KiwiGap(
                    duration_samples=max(0, int(round(missing_s * OUT_RATE))),
                    start_time=band.gap_from))
                band.gap_from = None
            out = rs.process(samples)
            q = band.quality
            q.batch_gaps = gaps
            if len(out) == 0:
                if gaps:
                    band.gap_from = gaps[0].start_time  # deliver with next batch
                return
            q.total_samples_delivered += len(out)
            band.sink.on_samples(out, q)

    def _rate_of(self, band: _Band) -> float:
        rate = band.channel.sample_rate if band.channel is not None else None
        return float(rate) if rate else float(OUT_RATE)

    def _set_producible(self, band: _Band, producible: bool) -> None:
        if band.producible is producible:
            return
        band.producible = producible
        cb = self._on_band_producible
        if cb is None:
            return
        try:
            cb(self.source_key, band.band_name, producible)
        except Exception:  # noqa: BLE001
            logger.exception("on_band_producible callback failed")
