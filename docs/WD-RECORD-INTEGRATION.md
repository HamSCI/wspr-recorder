# wd-record subprocess integration

**Status:** Design — not yet implemented.
**Author:** drafted 2026-05-23 in response to sustained multicast packet loss
on B4-100 traced to Python GIL contention in the MultiStream-Recv threads.

## Problem statement

wspr-recorder runs one Python `MultiStream-Recv` thread per radiod source.
On B4-100 (3 rx pre-bee2-disable, 2 rx now) we observed ~500-1500 events/min
of `ka9q.resequencer:Lost packet recovery` warnings.  Diagnosis:

- `%steal` is ~0% (not VM preemption)
- NIC RX errors are ~0 (not LAN packet loss)
- `/proc/net/snmp` shows **412 M UDP RcvbufErrors** since boot
- per-socket recv-Q sits at ~14 MB of the 16 MB rb cap
- `top -H`: ONE Python thread pegging 90% CPU while receiver threads sit
  at 0% — classic GIL-contention pattern
- `py-spy record`: hottest stacks are `malloc_trim`, `tracemalloc.take_snapshot`,
  `reprovision_stale → ensure_channel → _decode_status_response` — all
  GIL-held C/Python work that starves the receiver threads

The receivers can't drain their UDP sockets fast enough because the GIL
is held by other Python code.  Disabling memprofile + bee2 brought rate
from ~1000/min to ~85-200/min (5-10× improvement) but didn't eliminate it.

## Approach

Replace the in-process Python multicast receivers with a per-source
subprocess that runs ka9q-radio's existing `wd-record` C binary.
`wd-record` is GIL-free (it's C), already produces correctly-named WAV
files synced to UTC minute boundaries, and is the same recorder
wsprdaemon-bash v3 used.

Main process becomes the orchestrator:
- spawn `wd-record` per source at startup
- supervise (restart on crash)
- watch a spool directory for completed WAVs
- dispatch existing decoder pipeline against the spool files
- decoders, sink writes, uploaders stay in main process unchanged

## wd-record reminder

```
wd-record [OPTIONS] PCM_multicast_address

Key options for our use:
  -W  --wd_mode               WSPR mode: sync to UTC minute, JT8 filename format
  -d DIRECTORY                Output root
  -s  --subdirectories        ssrc/year/month/day/ tree
  -x MAXTIME                  Max file duration (seconds) — set to 120
  -t TIMEOUT                  Idle timeout (no samples → exit)
  -L MAXTIME                  Max process lifetime
```

Filename format already matches what wspr-recorder expects:
`YYYYMMDDTHHMMSSZ_<freq>_<...>.wav`

## File / process layout

```
wspr-recorder (PID N)
├── orchestrator thread
│   ├── for each source in config:
│   │   subprocess.Popen([wd-record, -W, -d SPOOL/source, multicast_addr...])
│   ├── monitors child PIDs, restarts on exit
│   └── inotify watch on SPOOL/*/<band>/ for WAV close-write events
├── decoder pool (existing, unchanged)
├── spot_sink + sink.db (existing, unchanged)
└── hs-uploader (existing, unchanged)

SPOOL=/dev/shm/wspr-recorder/wd-record-spool/
  └── <source-key>/             # e.g. radiod:B4-100-rx888mk2-status.local
      └── <ssrc>/                # numeric SSRC
          └── <yyyy>/<mm>/<dd>/
              └── YYYYMMDDTHHMMSSZ_<freq>_<...>.wav
```

## Migration plan

Gated by env var `WSPR_USE_WD_RECORD=1` (default off → no behavior change).
When enabled, wspr-recorder skips its own MultiStream + ReceiverManager
construction; in their place it spawns wd-record subprocesses and uses
the inotify-driven WAV watcher.

When disabled, current code path runs unchanged.  This lets us A/B
compare on the same host: run the in-process path, observe loss rate;
flip the env var, restart, observe again.

## Open questions

1. **One wd-record per source or one per band?**
   - Per-source: simpler (3 processes on B4-100); wd-record subscribes
     to one multicast group, handles all the SSRCs in that group
   - Per-band: more processes (51 on B4-100) but each is independent;
     a single misbehaving band can't block others
   - **Tentative:** per-source.  Matches current ReceiverManager
     granularity and keeps process count low.

2. **How do we pass multiple multicast addresses to wd-record?**
   wd-record currently takes one multicast addr per invocation.  Each
   source advertises its data multicast group; one per source is fine.

3. **What's the inotify watch granularity?**
   `inotify-tools` Python binding or pure-Python `pyinotify` or just
   poll directory mtime every 5 s.  Polling is dumb-but-reliable;
   inotify is event-driven but adds a dep.  **Tentative:** poll, since
   WAVs land on a 120s cadence and 5s latency on detection is fine.

4. **How do we coordinate with radiod's channel provisioning?**
   wspr-recorder's `ReceiverManager.connect()` does the
   `ensure_channel()` calls that tell radiod to start producing
   multicast for each band.  We still need this step — wd-record can
   only subscribe to multicast groups that radiod is actively
   producing.  So: keep ReceiverManager.connect() (the slow part is
   chrony gate + parallel provisioning, already optimized), but skip
   `start_streams()` (the in-process MultiStream creation).

5. **Cleanup of completed WAVs?**
   Currently BandRecorder keeps samples in memory; nothing on disk.
   With wd-record we get WAV files on disk.  After decode completes,
   delete the WAV.  Use `/dev/shm` so they're in tmpfs (fast, RAM-backed).
   B4-100 has 7.8 GB RAM; 51 channels × ~1.5 MB/min WAV × 2 min retention
   = ~150 MB peak — comfortably fits.

6. **What about psk-recorder?**
   psk-recorder has its OWN multicast subscriber.  If we want full GIL
   relief we'd need to migrate it too.  For this work, scope is
   wspr-recorder only.  psk-recorder is a separate package and can be
   migrated in a follow-up if the wspr-recorder PoC shows clear
   improvement.

## Implementation order

1. **PoC (~1 hour):** spawn wd-record on ONE source's multicast group,
   verify WAV files arrive in spool with correct naming + duration +
   sample rate.  No code changes — just shell.

2. **Skeleton (~2-3 hours):**
   - `wd_record_supervisor.py` — subprocess management, restart on crash
   - `wd_record_watcher.py` — directory poll → existing
     `BandRecorder._on_period_complete` callback
   - Wire into `__main__.py` behind `WSPR_USE_WD_RECORD` env gate
   - Unit tests for supervisor lifecycle

3. **Cutover (~1-2 hours):**
   - Bypass MultiStream in __main__.py when env gate is set
   - Replace `BandRecorder.on_samples` callback chain with WAV-arrival
     callback (much simpler — no buffering, no period detection)
   - Smoke test on B4-100 with one source first, then all sources

4. **Validation (~2-3 hours):**
   - Compare packet-loss rate: in-process vs wd-record paths
   - Compare spot acceptance rate at wsprnet/wsprdaemon-server
   - Compare RAM use (expect smaller — no Python sample queues)
   - Compare CPU breakdown (`top -H`, py-spy)

5. **Stretch:** retire the in-process MultiStream code once wd-record
   path is field-validated for ≥1 week.

## Expected outcome

GIL contention in receiver threads → eliminated (they're now C
processes).  Packet-loss rate should drop to near-zero unless the LAN
itself is dropping packets (which it isn't per current diagnostics).
RAM use should drop ~30 % (no Python in-memory sample queues).
Restart time should improve (fewer Python threads to spin up).
