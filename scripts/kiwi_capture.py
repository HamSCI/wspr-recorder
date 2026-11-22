#!/usr/bin/env python3
"""Record a KiwiSDR SND handshake and audio frames as test fixtures.

Read-only against the Kiwi: one audio channel, about 30 s, then a clean
close.  Writes two files into --out:

  handshake.jsonl  one JSON object per text exchange, in order:
                   {"t": <s since open>, "dir": "tx"|"rx", "text": "..."}
                   ("rx" lines are the server's non-SND frames decoded as
                   text, tag included, e.g. "MSG sample_rate=12000.000")
  snd_frames.bin   the first N binary SND frames exactly as received,
                   each preceded by a 4-byte big-endian length.

Usage:  kiwi_capture.py HOST PORT [--freq-khz 14095.6] [--seconds 30]
Needs websocket-client.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import time

import websocket

USER = "wspr-recorder_fixture-capture"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--freq-khz", type=float, default=14095.6)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--frames", type=int, default=50)
    ap.add_argument("--password", default="")
    ap.add_argument("--out", default=".")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    log = open(os.path.join(a.out, "handshake.jsonl"), "w")
    frames = open(os.path.join(a.out, "snd_frames.bin"), "wb")
    t0 = time.monotonic()

    def note(direction: str, text: str) -> None:
        log.write(json.dumps({"t": round(time.monotonic() - t0, 3),
                              "dir": direction, "text": text}) + "\n")
        log.flush()

    ws = websocket.create_connection(
        f"ws://{a.host}:{a.port}/{int(time.time() * 1000)}/SND", timeout=10)

    def send(text: str) -> None:
        note("tx", text)
        ws.send(text)

    send(f"SET auth t=kiwi p={a.password}")
    configured = False
    n_frames = 0
    last_keepalive = time.monotonic()
    try:
        while time.monotonic() - t0 < a.seconds:
            if time.monotonic() - last_keepalive >= 5.0:
                send("SET keepalive")
                last_keepalive = time.monotonic()
            opcode, data = ws.recv_data()
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                note("rx", "<close>")
                break
            if data[:3] == b"SND":
                if n_frames < a.frames:
                    frames.write(struct.pack(">I", len(data)) + data)
                    n_frames += 1
                continue
            text = data.decode("utf-8", "replace")
            note("rx", text)
            if not text.startswith("MSG"):
                continue
            for tok in text[3:].split():
                name, _, value = tok.partition("=")
                if name == "audio_rate":
                    send(f"SET AR OK in={value} out=44100")
                elif name == "sample_rate" and not configured:
                    configured = True
                    send(f"SET mod=usb low_cut=1340 high_cut=1660 "
                         f"freq={a.freq_khz:.3f}")
                    send("SET agc=0 hang=0 thresh=-100 slope=6 "
                         "decay=1000 manGain=60")
                    send("SET compression=0")
                    send(f"SET ident_user={USER}")
                elif (name in ("too_busy", "down", "redirect")
                      or (name == "badp" and value != "0")):
                    print("refused:", text)
    finally:
        try:
            ws.close()
        except Exception:
            pass
        log.close()
        frames.close()
    print(f"captured {n_frames} SND frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
