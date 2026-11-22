"""One KiwiSDR audio channel over the Kiwi's WebSocket SND stream.

Protocol subset (verified against a KiwiSDR 1 v1.902; fixtures in
tests/fixtures/kiwi/, recorded by scripts/kiwi_capture.py):

* open ``ws://host:port/<epoch_ms>/SND`` and send ``SET auth t=kiwi p=<pw>``;
* the server answers with ``MSG name=value ...`` frames.  ``badp=0`` means
  the password was accepted, ``badp`` non-zero means it was not;
  ``too_busy``/``down``/``redirect`` mean no channel for us;
* ``sample_rate=<float>`` is the true audio rate (crystal-dependent);
  ``audio_rate=<int>`` (with ``audio_init``) is answered with ``SET AR OK``;
* once both have arrived we send the wsprdaemon-equivalent receiver settings
  (USB, 1340-1660 Hz, AGC off, manual gain 60 dB, no compression) and our
  ident, then ``SET keepalive`` every 5 s;
* audio arrives as binary frames: ``b"SND"``, flags (1 byte), sequence
  (uint32 LE), S-meter (uint16 BE), then int16 PCM, big-endian unless the
  flags say little-endian (512 samples per frame on the recorded Kiwi).
"""
from __future__ import annotations

import logging
import struct
import threading
import time
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

RATE_MIN_HZ = 11990.0
RATE_MAX_HZ = 12010.0

# SND flag bits (Kiwi server snd.h).
_SND_FLAG_COMPRESSED = 0x10
_SND_FLAG_LITTLE_ENDIAN = 0x80

_SND_HEADER = 10  # tag(3) + flags(1) + seq(4) + smeter(2)

# A sequence jump larger than this is a stream restart, not lost frames:
# resynchronise rather than inventing seconds of zeros.
MAX_GAP_FRAMES = 120


class KiwiChannel:
    """Receive one WSPR dial frequency from a KiwiSDR as 12 kHz audio.

    ``on_audio(samples, rx_monotonic)`` gets float32 samples (int16/32768)
    per SND frame; a lost frame is delivered as zeros of the same length.
    ``on_state(state, detail)`` reports ``connected``, ``refused``,
    ``auth_failed``, ``dropped`` or ``bad_rate``.  Reconnection is the
    caller's business: after any terminal state the thread ends.
    """

    KEEPALIVE_S = 5.0

    def __init__(self, host: str, port: int, freq_hz: int, *,
                 password: str = "", user: str,
                 on_audio: Callable[[np.ndarray, float], None],
                 on_state: Callable[[str, str], None]):
        self.host = host
        self.port = port
        self.freq_hz = freq_hz
        self.password = password
        self.user = user
        self._on_audio = on_audio
        self._on_state = on_state

        self._sample_rate: Optional[float] = None
        self._audio_init = False
        self._configured = False
        self._finished = False          # a terminal state was reported
        self._stopping = threading.Event()
        self._next_seq: Optional[int] = None
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._keepalive_thread: Optional[threading.Thread] = None

    @property
    def sample_rate(self) -> Optional[float]:
        """The audio sample rate the Kiwi reported, or None before it has."""
        return self._sample_rate

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        import websocket

        url = f"ws://{self.host}:{self.port}/{int(time.time() * 1000)}/SND"
        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._handle_open,
            on_message=self._handle_message,
            on_error=self._handle_error,
            on_close=self._handle_close,
        )
        self._thread = threading.Thread(
            target=self._ws.run_forever, kwargs={"ping_interval": 0},
            name=f"kiwi-{self.host}:{self.port}-{self.freq_hz}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                logger.debug("kiwi close failed", exc_info=True)
        for t in (self._thread, self._keepalive_thread):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=5)

    # ---- WebSocket handlers --------------------------------------------

    def _handle_open(self, ws) -> None:
        self._ws = ws
        ws.send(f"SET auth t=kiwi p={self.password}")

    def _handle_message(self, ws, message) -> None:
        if self._finished:
            return
        if isinstance(message, str):
            message = message.encode()
        if message[:3] == b"SND":
            self._handle_snd(message)
        elif message[:3] == b"MSG":
            self._handle_msg(ws, message[3:].decode("utf-8", "replace"))

    def _handle_error(self, ws, error) -> None:
        if not self._stopping.is_set():
            logger.warning("kiwi %s:%s %.3f kHz: %s", self.host, self.port,
                           self.freq_hz / 1000, error)

    def _handle_close(self, ws, status_code=None, reason=None) -> None:
        if self._stopping.is_set() or self._finished:
            return
        self._finish(ws, "dropped", f"{status_code} {reason or ''}".strip())

    # ---- protocol ------------------------------------------------------

    def _handle_msg(self, ws, body: str) -> None:
        for token in body.split():
            name, _, value = token.partition("=")
            if name == "too_busy" or name == "down" or name == "redirect":
                self._finish(ws, "refused", token)
                return
            if name == "badp":
                if value != "0":
                    self._finish(ws, "auth_failed", token)
                    return
            elif name == "sample_rate":
                rate = float(value)
                if not RATE_MIN_HZ <= rate <= RATE_MAX_HZ:
                    self._finish(ws, "bad_rate", str(rate))
                    return
                self._sample_rate = rate
            elif name == "audio_init":
                self._audio_init = True
            elif name == "audio_rate":
                ws.send(f"SET AR OK in={int(value)} out=44100")
        if (not self._configured and self._audio_init
                and self._sample_rate is not None):
            self._configure(ws)

    def _configure(self, ws) -> None:
        self._configured = True
        ws.send(f"SET mod=usb low_cut=1340 high_cut=1660 "
                f"freq={self.freq_hz / 1000:.3f}")
        ws.send("SET agc=0 hang=0 thresh=-100 slope=6 decay=1000 manGain=60")
        ws.send("SET compression=0")
        ws.send(f"SET ident_user={self.user}")
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, args=(ws,),
            name=f"kiwi-keepalive-{self.freq_hz}", daemon=True)
        self._keepalive_thread.start()
        self._on_state("connected", f"sample_rate={self._sample_rate}")

    def _keepalive_loop(self, ws) -> None:
        while not self._stopping.wait(self.KEEPALIVE_S):
            if self._finished:
                return
            try:
                ws.send("SET keepalive")
            except Exception:
                return  # the close handler reports the drop

    def _handle_snd(self, frame: bytes) -> None:
        rx_monotonic = time.monotonic()
        if not self._configured or len(frame) < _SND_HEADER:
            return
        flags = frame[3]
        (seq,) = struct.unpack_from("<I", frame, 4)
        if flags & _SND_FLAG_COMPRESSED:
            logger.warning("kiwi %s:%s sent a compressed SND frame; dropped",
                           self.host, self.port)
            return
        dtype = "<i2" if flags & _SND_FLAG_LITTLE_ENDIAN else ">i2"
        pcm = np.frombuffer(frame, dtype=dtype, offset=_SND_HEADER)
        samples = pcm.astype(np.float32) / np.float32(32768.0)

        if self._next_seq is not None and seq != self._next_seq:
            missing = (seq - self._next_seq) & 0xFFFFFFFF
            if 0 < missing <= MAX_GAP_FRAMES:
                for _ in range(missing):
                    self._on_audio(np.zeros(len(samples), np.float32),
                                   rx_monotonic)
            else:
                logger.warning("kiwi %s:%s SND sequence jumped %d -> %d; "
                               "resynchronising", self.host, self.port,
                               self._next_seq, seq)
        self._next_seq = (seq + 1) & 0xFFFFFFFF
        self._on_audio(samples, rx_monotonic)

    def _finish(self, ws, state: str, detail: str) -> None:
        self._finished = True
        self._stopping.set()
        try:
            ws.close()
        except Exception:
            logger.debug("kiwi close failed", exc_info=True)
        self._on_state(state, detail)
