"""KiwiChannel protocol tests, replayed from frames recorded off a real Kiwi.

Fixtures (tests/fixtures/kiwi/, recorded by scripts/kiwi_capture.py from
AI6VN-2 against the lab KiwiSDR 1 v1.902 at 10.22.23.70:8073):
  handshake.jsonl  the text exchange, in order ("rx" = server, "tx" = us)
  snd_frames.bin   the first 50 SND frames, each with a 4-byte BE length
"""
from __future__ import annotations

import json
import struct
import time
from pathlib import Path

import numpy as np
import pytest

from wspr_recorder.kiwi_channel import KiwiChannel

FIX = Path(__file__).parent / "fixtures" / "kiwi"


def _server_messages() -> list[bytes]:
    out = []
    for line in (FIX / "handshake.jsonl").read_text().splitlines():
        rec = json.loads(line)
        if rec["dir"] == "rx" and rec["text"] != "<close>":
            out.append(rec["text"].encode())
    return out


def _snd_frames() -> list[bytes]:
    blob = (FIX / "snd_frames.bin").read_bytes()
    frames, i = [], 0
    while i < len(blob):
        (n,) = struct.unpack(">I", blob[i:i + 4])
        frames.append(blob[i + 4:i + 4 + n])
        i += 4 + n
    return frames


def _recorded_sample_rate() -> float:
    for m in _server_messages():
        if m.startswith(b"MSG sample_rate="):
            return float(m.split(b"=", 1)[1])
    raise AssertionError("fixture has no sample_rate")


class FakeWS:
    def __init__(self):
        self.sent: list[str] = []
        self.closed = False

    def send(self, text):
        self.sent.append(text)

    def close(self):
        self.closed = True


class Recorder:
    def __init__(self):
        self.audio: list[np.ndarray] = []
        self.states: list[tuple[str, str]] = []

    def on_audio(self, samples, rx_monotonic):
        self.audio.append(samples)

    def on_state(self, state, detail):
        self.states.append((state, detail))


def _channel(rec: Recorder, freq_hz: int = 14_095_600) -> KiwiChannel:
    return KiwiChannel("10.22.23.70", 8073, freq_hz,
                       user="wspr-recorder_v0.1.0",
                       on_audio=rec.on_audio, on_state=rec.on_state)


def _replay(ch: KiwiChannel, ws: FakeWS, messages, frames=()):
    ch._handle_open(ws)
    for m in messages:
        ch._handle_message(ws, m)
    for f in frames:
        ch._handle_message(ws, f)


def test_parses_sample_rate_from_handshake():
    rec, ws = Recorder(), FakeWS()
    ch = _channel(rec)
    assert ch.sample_rate is None
    _replay(ch, ws, _server_messages())
    assert ch.sample_rate == pytest.approx(11999.020338, abs=0.01)
    assert ch.sample_rate == pytest.approx(_recorded_sample_rate(), abs=1e-9)
    assert "connected" in [s for s, _ in rec.states]
    assert not ws.closed


def test_snd_frame_to_float32_int16_scale():
    rec, ws = Recorder(), FakeWS()
    ch = _channel(rec)
    frames = _snd_frames()
    _replay(ch, ws, _server_messages(), frames[:1])
    assert len(rec.audio) == 1
    got = rec.audio[0]
    # Recorded layout: "SND" + flags(1) + seq(4, LE) + S-meter(2, BE) + PCM.
    pcm = np.frombuffer(frames[0][10:], dtype=">i2")
    assert len(got) == len(pcm) == 512
    assert got.dtype == np.float32
    np.testing.assert_array_equal(got, pcm.astype(np.float32) / 32768.0)


def test_all_recorded_frames_decode_in_order():
    rec, ws = Recorder(), FakeWS()
    ch = _channel(rec)
    frames = _snd_frames()
    _replay(ch, ws, _server_messages(), frames)
    assert len(rec.audio) == len(frames) == 50
    assert all(len(a) == 512 for a in rec.audio)


def test_sends_wd_equivalent_settings():
    rec, ws = Recorder(), FakeWS()
    ch = _channel(rec)
    _replay(ch, ws, _server_messages())
    assert ws.sent[0] == "SET auth t=kiwi p="
    sent = "\n".join(ws.sent)
    assert "SET mod=usb low_cut=1340 high_cut=1660 freq=14095.600" in sent
    assert "SET agc=0 hang=0 thresh=-100 slope=6 decay=1000 manGain=60" in sent
    assert "SET compression=0" in sent
    assert "SET ident_user=wspr-recorder_v0.1.0" in sent
    assert "SET AR OK in=12000 out=44100" in sent


def test_password_is_sent_in_auth():
    rec, ws = Recorder(), FakeWS()
    ch = KiwiChannel("h", 8073, 7_038_600, password="s3cret", user="u",
                     on_audio=rec.on_audio, on_state=rec.on_state)
    ch._handle_open(ws)
    assert ws.sent == ["SET auth t=kiwi p=s3cret"]


def test_too_busy_reports_refused():
    rec, ws = Recorder(), FakeWS()
    ch = _channel(rec)
    _replay(ch, ws, [b"MSG too_busy=8"], _snd_frames()[:3])
    assert rec.states and rec.states[0][0] == "refused"
    assert rec.audio == []
    assert ws.closed


def test_badp_reports_auth_failed():
    rec, ws = Recorder(), FakeWS()
    ch = _channel(rec)
    _replay(ch, ws, [b"MSG badp=1"], _snd_frames()[:3])
    assert rec.states and rec.states[0][0] == "auth_failed"
    assert rec.audio == []
    assert ws.closed


def test_badp_zero_is_success():
    # The real handshake carries "MSG badp=0" when the password is accepted.
    assert b"MSG badp=0" in _server_messages()
    rec, ws = Recorder(), FakeWS()
    _replay(_channel(rec), ws, _server_messages())
    assert "auth_failed" not in [s for s, _ in rec.states]


def test_rate_outside_11990_12010_reports_bad_rate():
    rec, ws = Recorder(), FakeWS()
    ch = _channel(rec)
    msgs = [b"MSG sample_rate=20250.000000" if m.startswith(b"MSG sample_rate=")
            else m for m in _server_messages()]
    _replay(ch, ws, msgs, _snd_frames()[:3])
    assert ("bad_rate", "20250.0") in rec.states
    assert "connected" not in [s for s, _ in rec.states]
    assert rec.audio == []
    assert ws.closed


def test_sequence_gap_fills_zeros():
    rec, ws = Recorder(), FakeWS()
    ch = _channel(rec)
    f = _snd_frames()
    _replay(ch, ws, _server_messages(), [f[0], f[1], f[3]])  # seq 3 missing
    assert len(rec.audio) == 4
    assert len(rec.audio[2]) == 512
    assert not rec.audio[2].any()
    assert rec.audio[3].any()
    assert not ws.closed
    assert "dropped" not in [s for s, _ in rec.states]


def test_unexpected_close_reports_dropped_but_stop_does_not():
    rec, ws = Recorder(), FakeWS()
    ch = _channel(rec)
    _replay(ch, ws, _server_messages())
    ch._handle_close(ws, 1006, "gone")
    assert rec.states[-1][0] == "dropped"

    rec2, ws2 = Recorder(), FakeWS()
    ch2 = _channel(rec2)
    _replay(ch2, ws2, _server_messages())
    ch2.stop()
    ch2._handle_close(ws2, 1000, "")
    assert "dropped" not in [s for s, _ in rec2.states]


def test_close_without_detail_says_connection_closed():
    rec, ws = Recorder(), FakeWS()
    ch = _channel(rec)
    _replay(ch, ws, _server_messages())
    ch._handle_close(ws, None, None)
    assert rec.states[-1] == ("dropped", "connection closed")


def test_keepalive_sent_periodically_once_connected():
    rec, ws = Recorder(), FakeWS()
    ch = _channel(rec)
    ch.KEEPALIVE_S = 0.02
    _replay(ch, ws, _server_messages())
    time.sleep(0.15)
    ch.stop()
    assert ws.sent.count("SET keepalive") >= 2


@pytest.mark.parametrize("code,meaning", [
    (2, "still determining local interface address"),
    (3, "not allowed from this IP"),
    (5, "multiple connections from the same IP not allowed"),
    (6, "database update in progress"),
    (9, "unknown"),
])
def test_badp_other_than_1_is_refused_not_auth_failed(code, meaning):
    """Only badp=1 is a wrong password (Beagle_SDR_GPS web/kiwi/kiwi.js
    BADP_TRY_AGAIN); the other codes are transient or policy refusals,
    retried like too_busy, with the code's meaning in the detail."""
    rec, ws = Recorder(), FakeWS()
    ch = _channel(rec)
    _replay(ch, ws, [f"MSG badp={code}".encode()], _snd_frames()[:3])
    assert rec.states and rec.states[0][0] == "refused"
    assert f"badp={code}" in rec.states[0][1]
    assert meaning in rec.states[0][1]
    assert rec.audio == []
    assert ws.closed
