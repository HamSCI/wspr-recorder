# wspr-recorder

WSPR / FST4W recorder, decoder, and uploader for [ka9q-radio][ka9q].
Connects to a `radiod` instance over RTP multicast, keeps a per-band
float32 ring buffer, emits peak-normalized int16 WAVs at epoch-aligned
decode-period boundaries (120 s, 300 s, 900 s, 1800 s), and — when the
pipeline-v2 feature flags are enabled — decodes those WAVs in-process
with `wsprd` / `jt9`, writes spots and per-cycle noise into sigmond's
local SQLite sink, and ships them upstream to wsprdaemon.org, which
forwards them to wsprnet.org and pskreporter.info.  The station posts to
neither itself; see **Uploads** below.

wspr-recorder now supersedes `wsprdaemon-client`'s WSPR recording,
decoding, and uploading role: with `WD_DECODE_VIA_DB=1` and
`WSPR_USE_HS_UPLOADER=1` set, a single `wspr-recorder@<id>.service`
covers the whole path from RTP samples to accepted spots, with no
`wd-decode@*` / `wd-post@*` / `wd-upload-*` units involved.

```
radiod (ka9q-radio)
  │   RTP multicast, one stream per band channel (f32 wire, 12 kHz)
  ▼
wspr-recorder daemon (one per radiod)
  ├─ MultiStream per multicast group (ka9q-python)
  ├─ per-band: float32 ring buffer → slice at period boundary
  │             → peak-normalize → int16 WAV + JSON sidecar
  │             → /dev/shm/wspr-recorder/<band>/
  │
  ├─ decode (WD_DECODE_VIA_DB=1): wsprd + jt9 --fst4w per cycle
  │             → wspr.spots + wspr.noise rows
  │             → /var/lib/sigmond/sink.db  (hs_uploader.sink)
  │
  └─ upload (WSPR_USE_HS_UPLOADER=1): in-process hs-uploader
                → wsprdaemon.org (cycle-aligned tar via SFTP)
                     → wsprnet.org, forwarded by the server
                     → pskreporter.info, forwarded by the server
```

One `wspr-recorder@<radiod_id>.service` instance per radiod. The
instance id is derived from `status_address` by stripping
`-status.local` / `.local`. Contract follows HamSCI sigmond
[client contract v0.8][contract]. The unit is `Type=notify` with
`WatchdogSec` — the daemon sends `READY=1` on startup and pets the
watchdog while running.

## Operating modes

wspr-recorder runs in one of two modes, selected by environment
variables in the unit's env file (see [docs/CONFIG.md](docs/CONFIG.md)):

- **Recorder-only (default).** No env flags set. wspr-recorder records
  WAV + JSON sidecar pairs to `/dev/shm/wspr-recorder/<band>/` and
  nothing else. A separate consumer (legacy `wsprdaemon-client`
  `wd-decode@*` chain, or any other) tails the spool and handles
  decoding and uploading.

- **Full pipeline.** `WD_DECODE_VIA_DB=1` enables in-process decode to
  the SQLite sink; `WSPR_USE_HS_UPLOADER=1` additionally enables the
  in-process uploader. Each flag is independent and each is a no-op if
  its prerequisites (sigmond's `hamsci_sink`, the `hs-uploader` package,
  reporter identity env vars) are missing — the recorder always runs.

## Quickstart

```bash
git clone https://github.com/HamSCI/wspr-recorder /opt/git/sigmond/wspr-recorder
sudo /opt/git/sigmond/wspr-recorder/install.sh
sudoedit /etc/wspr-recorder/config.toml   # set status_address, bands
sudo systemctl start wspr-recorder@<radiod_id>
journalctl -fu wspr-recorder@<radiod_id>
wspr-ctl health
```

wspr-recorder resolves the status multicast of a `radiod` on the LAN via
mDNS.  When it cannot find that radiod, it tells systemd it has started
and keeps trying, 5 s after the first miss and then up to every 60 s,
rather than exiting. Decoders (`wsprd`, `jt9`) are
only needed when running the full pipeline — see
[docs/INSTALL.md](docs/INSTALL.md).

For tests:

```bash
pip install -e ".[dev]"
pytest tests/
```

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — full install: deps, systemd, paths, Pattern A
- [docs/CONFIG.md](docs/CONFIG.md) — TOML schema + environment-variable reference
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — running it: logs, `wspr-ctl`, common failures
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — internals for contributors
- [docs/SIGMOND-CONTRACT.md](docs/SIGMOND-CONTRACT.md) — how wspr-recorder satisfies contract v0.8
- [docs/archive/PHASE-2-COORDINATION.md](docs/archive/PHASE-2-COORDINATION.md) — historical: the DB-direct decode decision record
- [CLAUDE.md](CLAUDE.md) — development briefing

## What it does

**Records:** receives RTP multicast from `radiod` (float32 at the
callback boundary, regardless of wire encoding), maintains one float32
ring buffer per configured band sized to that band's longest decode
period plus 120 s headroom, detects minute boundaries via a
timing-aware sync strategy (RTP-clock, wall-clock, or fallback), slices
the ring at epoch-aligned period boundaries (W2, F2, F5, F15, F30),
peak-normalizes float32 → int16 at WAV-write time, and emits `.wav` +
`.json` sidecar pairs under `/dev/shm/wspr-recorder/<band>/`
atomically.  The recorder skips a band that lies above the radiod's
Nyquist frequency (8 m and 6 m on a 64.8 Msps radiod).  Neither the cycle
commit nor the channel-health check waits for a skipped band; it rejoins
once the radiod's rate lets it.

**Decodes** (when `WD_DECODE_VIA_DB=1`): runs `wsprd` (2-pass) and
`jt9 --fst4w` on each cycle's WAV, resolves type-3 callsign hashes
across decoders via a persistent `CallsignDB`, measures per-cycle RMS
and FFT noise, and writes `wspr.spots` + `wspr.noise` rows into
sigmond's local SQLite sink (`/var/lib/sigmond/sink.db`) through
`hs_uploader.sink.Writer`, or `sigmond.hamsci_sink` when hs-uploader
lacks it.

**Uploads** (when `WSPR_USE_HS_UPLOADER=1`): runs an in-process
`hs-uploader` pump that reads the sink and ships a cycle-aligned
spots+noise tar to wsprdaemon.org via SFTP.  The tar also carries the
host's psk-recorder and meteor-scatter rows (FT8/FT4, MSK144).

HamSCI's standard since 2026-11-13: every spot goes only to
wsprdaemon.org, which forwards copies to wsprnet.org and
pskreporter.info under each spot's own reporter id.  This repo's pump
(`hs_uploader_shim.py`) follows it.  The pump builds no wsprnet.org
pipeline.  Its tar transport sets `forward_to_wsprnet`, and each FT8/FT4
row carries its own `forward_to_pskreporter` flag, which the tar's
`routing.json` collects.  The pump sends no reporter identity, so the
server posts under each spot's own call.  Sigmond does the same for the
single-host `hs-uploader` daemon: its rendered manifest flags the
`wspr-wsprdaemon` tar and leaves out the `wspr-wsprnet` pipeline that
`deploy.toml` declares.  The per-cycle journal line reports
`wsprnet=0`, since the station posts nothing there itself.

The uploader can also run on its own.  A single recorder can keep it
in process.  A merge fleet, with several receivers writing one sink,
runs exactly one standalone `wspr-uploader.service` for all of them.
Each recorder sends that unit a wake datagram on `upload-wake.sock`
after every commit.

**Does not:** choose multicast destinations — radiod advertises those
via its status stream — and does not apply chain-delay correction
(WSPR is minute-quantized, far outside that regime; the value is
surfaced in `inventory --json` only).

## License

MIT. See [LICENSE](LICENSE). Author: Michael Hauan, AC0G.

[ka9q]: https://github.com/ka9q/ka9q-radio
[contract]: https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md
