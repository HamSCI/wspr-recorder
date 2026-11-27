# Carrier archive

> **Audience:** operator/contributor
> **Status:** new 2026-11-24, off by default, not yet run on a live station; WW0WWV-only beta 2026-11-25; WWV measured on hf-timestd's raw IQ since 2026-11-25

An optional archive of WSPR / FST4W spots' **carriers**, plus a small
read-only web page to browse it. Both are **disabled by default**.

**Release note, 2026-11-25: enabling the archive now keeps only WW0WWV
unless `calls` is set.** Before, `enabled = true` kept every call; set
`calls = ["*"]` to keep doing that.

**Beta scope: WW0WWV only.** By default only the carriers of **WW0WWV**
(the WWV-site WSPR beacon) are kept, and the browser's main page compares
them with WWV's own carrier Doppler. Every other call's carrier is
discarded as soon as it is decoded. `calls = ["*"]` keeps every call (see
[Which calls are kept](#which-calls-are-kept)).

## What it is

Once wsprd or jt9 has decoded a spot, it knows exactly which tones were
sent. The ubersdr decoder builds can then multiply the received samples by
the conjugate of that decoded signal. That removes the modulation and leaves
the bare carrier, which shows what the propagation path did to the signal
over the whole transmission: its Doppler shift, its spread, and its fading.
A spot reports only one frequency, at the middle of the transmission.

For each decode the decoder writes a small `.wav`:

- 2 channels (I, Q), 32-bit float, at **15 Hz**. That is ±7.5 Hz around the
  decoder's frequency, from the start of the transmission to its end.
- The decode's details (message, SNR, dt, drift, reference and fine
  frequency, w50 spread, start offset) are stored as JSON in the WAV's
  `LIST/INFO/ICMT` comment.

The decoder names it `CALL_AUDIOHZ.wav`, or `CALL_AUDIOHZ_YYMMDD_HHMM.wav`
if the decoder puts the period in the name (proposed upstream after PR #8,
so several periods can share one directory). wspr-recorder accepts both. It
gives each decoder run its own scratch directory for one cycle, and takes
the date and time from the cycle, not from the name.

wspr-recorder files each one as:

```
<root>/YYYYMMDD/RECEIVER/BAND/HHMM_MODE_CALL_RFHZ.wav
```

| Part | Meaning |
|---|---|
| `YYYYMMDD`, `HHMM` | UTC date and start of the cycle |
| `RECEIVER` | the instance's reporter id (e.g. `AC0G-B1`), else the radiod name. Made filesystem-safe: `/` becomes `-`, so `AC0G/S` is `AC0G-S`. With several `[[source]]`s, `_<source>` is appended. |
| `BAND` | the band directory name (`20`, `80eu`, `630`, ...) |
| `MODE` | `W2`, `F2`, `F5`, `F15`, `F30` |
| `CALL` | the decoded call. `<...>` is removed and any other non-alphanumeric character becomes `_`, so `PJ4/K1ABC` is `PJ4_K1ABC`. Unresolved hashes are not written. |
| `RFHZ` | dial + the decoder's audio frequency, to 0.001 Hz. For WSPR this is the centre; for FST4W it is the lowest tone. |

This is the same layout as wsprdaemon's `carrier-archive.sh`. When more
than one wsprd pass writes a carrier for the same call, the carrier from the
`-n` (no-drift, spreading) pass is kept. That is the same rule
`_merge_wspr_passes` uses for the spot itself. The spreading pass is retired
at present, so only one pass runs.

## Requirements

The ubersdr decoders at **e8c2c52 or later** (madpsy ubersdr_wsprd PR #8,
merged 2026-11-27; sigmond pinned Rob's fork at 668cfd2 before that),
which add:

- `wsprd -I DIR`: usage line `-I dir: write each decode's carrier, ...`
- `jt9 --fst4w --carrier-dir DIR`

The recorder checks each binary's help text once (per path and mtime)
before passing an option, using the same check as `-F` / `--fine-freq`. If
the decoders are older, or are the stock WSJT-X ones, the option is never
passed, nothing is archived, and decoding is exactly as before. The
recorder also needs the in-process decode path (`WD_DECODE_VIA_DB=1`, the
sigmond default).

## Enabling it

Add the following to the instance's config (`/etc/wspr-recorder/<instance>.toml`):

```toml
[carrier_archive]
enabled = true
# root = "/var/lib/wspr-recorder/carrier-archive"   # the default
# max_age_days = 3                                   # whole UTC days kept, today included
# max_disk_pct = 90                                  # then oldest days go while the fs is fuller
# calls = ["WW0WWV"]                                 # the default; ["*"] or [] keeps every call
```

Then restart `wspr-recorder@<instance>`. Notes:

- The default `root` is under the unit's `StateDirectory`
  (`/var/lib/wspr-recorder`). It is on disk, and the recorder's sandbox can
  already write there. Do **not** point it at `/dev/shm`: that is RAM. If
  you choose another directory, add it to the unit's `ReadWritePaths=` with
  a drop-in.
- Invalid values (for example `max_age_days = 0`, or a relative `root`)
  stop the config from loading. They are checked even while
  `enabled = false`. `root` may not be `/`, may not be (or contain)
  `/var/lib/sigmond`, `/var/lib/wspr-recorder` or the recorder's
  `output_dir`, and may not sit inside `output_dir`.
- The recorder writes a `.wspr-carrier-archive` marker file into `root`
  when it creates it, or when it first uses an existing root that is empty
  or holds only day directories. Purge does nothing, and logs one warning,
  in a root without the marker.
- Purging runs on the recorder's 5-minute cleanup timer, never during a
  decode, and only when the archive is enabled. First, any day directory
  older than `max_age_days`, or dated after today (a clock jump), is
  deleted. Then, while the filesystem holding `root` is more than
  `max_disk_pct` full, the oldest day before today is deleted. Today's
  directory is never deleted for disk use.
- Hard limit: while the filesystem is at `max_disk_pct + 5` (at most 98%)
  or above, carriers are not filed at all (one warning, and one line when
  filing resumes).
- Errors while filing or purging are logged (`carrier archive: ...`), at
  most once per cycle, and ignored. They never stop a decode or an upload.
- Decoders write into hidden scratch dirs (`<band>/.phase2/carrier.d/.p1.XXXX`)
  that the WAV cleanup never touches; leftovers from a crash are swept
  when the recorder starts.

## Which calls are kept

`calls` lists the callsigns whose carriers are filed. The default is
`["WW0WWV"]`. Every other carrier is discarded straight from the decoder's
scratch directory; nothing of it reaches the archive.

- Matching is on the **base call**, ignoring case: `WW0WWV/P`,
  `ww0wwv` and `PJ4/WW0WWV` all count as `WW0WWV`.
- `calls = []` or `calls = ["*"]` keeps **every** call (the archive as it
  was before the beta).
- Entries must be letters, digits and `/`, or `"*"`; anything else stops
  the config from loading.

The browser's main page is about WW0WWV whatever `calls` says. With more
calls kept, the other spots appear on its **All spots** page.

## Disk use

A carrier file holds 15 samples/s × 8 bytes for the length of the
transmission:

| Mode | Per spot |
|---|---|
| W2, F2 | ≈ 13–15 KB |
| F5 | ≈ 36 KB |
| F15 | ≈ 108 KB |
| F30 | ≈ 216 KB |

With the default `calls = ["WW0WWV"]` the archive is tiny: WW0WWV sends
on a handful of bands every 2 minutes, so at most a few thousand files a
day (a few tens of MB). The estimates below are for `calls = ["*"]`.

WSPR-2 dominates. Estimates:

| Station | WSPR spots/day | Per day | 3 days (default) |
|---|---|---|---|
| quiet, few bands | 5,000 | ≈ 65 MB | ≈ 200 MB |
| busy, 14–17 bands | 50,000 | ≈ 650 MB | ≈ 2 GB |
| very busy / several receivers | 150,000 | ≈ 2 GB | ≈ 6 GB |

To estimate your own station, count a day's spots
(`smd wspr watch`, or the sink's `wspr.spots`) and multiply by 13 KB.
`max_disk_pct` is a backstop, not a budget. Set `max_age_days` so the
archive fits comfortably.

## Browsing it

`wspr_recorder/carrier_browser.py` serves a read-only page:

```
python -m wspr_recorder.carrier_browser --root DIR [--bind ADDR] [--port 8089]
    [--raw-buffer DIRS] [--wwv-cache-dir DIR] [--wwv-prefill 0|1] [--timestd-db PATH]
```

It uses the Python standard library and numpy. Charts are drawn in the
browser from the page's own JSON; nothing is loaded from a CDN.

### Main page: WW0WWV vs WWV

WW0WWV is the WSPR beacon at the WWV site. Its carrier and WWV's own
carrier cross nearly the same ionosphere, so their Doppler shifts, in
parts per billion (ppb) of each nominal frequency, should move together.

For the selected UTC day (today by default; ◀ ▶ step through the archive's
days) the page shows one card per band pair:

| WW0WWV band | WW0WWV nominal, Hz | compared with |
|---|---|---|
| 80 m | 3 570 050.145432 | WWV 2.5 MHz |
| 40 m | 7 040 050.130209 | WWV 5 MHz |
| 30 m | 10 140 150.146485 | WWV 10 MHz |
| 20 m | 14 097 050.048829 | WWV 15 MHz |
| 15 m | 21 096 050.048829 | WWV 20 MHz |
| 12 m | 24 926 049.919536 | WWV 25 MHz |
| 10 m | 28 126 050.109966 | WWV 25 MHz |

The nominals are the KPH study's (`fst4w-freq-test/kph`). The 12 m value is
the WS-8's own, not the 24 926 050.049 Hz of the first tables, which is
5.19 ppb off.

Each card has:

- **Doppler, ppb from nominal, over the day.** Orange squares are WW0WWV:
  one point per WSPR-2 cycle, the whole transmission's frequency (the
  periodogram peak of the carrier, zero-padded 64×, with a parabola
  through the top three bins). Its RF is `dial_mhz × 1e6 + ref_hz` from the
  file's JSON, plus that peak. The blue line is WWV, one value per minute,
  measured on hf-timestd's raw IQ (see below). Faint grey dots are minutes
  too weak or at the edge of the search window (not used); small blue
  rings are a second carrier. The x axis is UTC, with PST underneath.
- **SNR over the day.** WW0WWV's is wsprd's (dB in 2500 Hz). WWV's is its
  carrier peak over the median noise of the minute's spectrum, per 1/60 Hz
  bin, typically 60–90 dB. The two are different measures, so compare
  their trends, not their levels.
- **A summary line:** the cycle count, where WWV's data came from (or
  **no WWV data** and why), **WWV+WWVH** on shared frequencies, the
  minutes with two carriers, and the median WW0WWV − WWV difference.

**Click a WW0WWV point** to open that cycle under the card:

- WW0WWV's carrier spectrogram (±1.5 Hz around the nominal);
- its Doppler track (10 s windows every 2 s, or 30 s windows when wsprd's
  SNR is −15 dB or less), with WWV's minute values drawn as bars over the
  same two minutes: two one-minute points per cycle (a second carrier as a
  thin bar);
- the SNR track;
- the numbers: WW0WWV's ppb, WWV's ppb over the same minutes, and their
  difference.

◀ ▶ step through the cycles. There are links to download the `.wav` and
to the cycle on the All spots page. The URL (`#date=…&band=…&hhmm=…`)
keeps the selection.

Only WSPR-2 (`W2`) carriers are used here. The archive's FST4W files name
the lowest tone, not the centre. If a day has WW0WWV from more than one
receiver, a **Receiver** menu appears.

### Where WWV's Doppler comes from

**Since 2026-11-25 WWV's carrier is measured directly on hf-timestd's raw
IQ.** Before, the page read `doppler_hz` from hf-timestd's
`L2_tick_timing` table. On hf-timestd 5.0.0 that value is **noise**: on
KPH1, over 24 h, on all six channels, it had mean ≈ 0, standard deviation
0.04–0.05 Hz and lag-1 autocorrelation 0.00, and it did not follow the
real carrier at all. Over the same minutes (17:40–17:49Z) the carrier
itself, 60–86 dB above the noise, moved smoothly from −0.155 to +0.098 Hz
on WWV 20 MHz, sat at +0.01..+0.19 Hz on 10 MHz and at 0 ± 0.01 Hz on
5 MHz.

**The raw buffers.** hf-timestd keeps every channel's IQ, ten minutes to
a file, for about two days:

```
/var/lib/timestd/raw_buffer/<CHANNEL>/<YYYYMMDD>/<epoch>.bin.zst   complex64, little-endian, zstd
/var/lib/timestd/raw_buffer/<CHANNEL>/<YYYYMMDD>/<epoch>.json      minute_boundary, sample_rate,
                                                                   frequency_hz, completeness_pct, ...
```

with a live copy under `/dev/shm/timestd/raw_buffer`. The channels are
`SHARED_2500`, `SHARED_5000`, `SHARED_10000`, `SHARED_15000`, `WWV_20000`
and `WWV_25000` (the page looks for `WWV_<kHz>`, then `SHARED_<kHz>`).
radiod tunes each channel to the nominal frequency (`frequency_hz`, e.g.
20000000), so the carrier sits at 0 Hz and its frequency is its offset
from nominal. A channel whose `frequency_hz` differs from nominal is
corrected by the difference.

**The measurement** (`wspr_recorder/wwv_raw.py`, from the KPH session's
`wwvcar.py`):

1. Average blocks of 240 samples (a boxcar): 24 kHz becomes 100 Hz. The
   boxcar's nulls at multiples of 100 Hz also remove WWV's 100 Hz time
   code and its audio tones.
2. For each UTC minute: 6000 samples, a Hann window, a 65536-point FFT.
3. The peak within **±1.5 Hz** of nominal, refined by a parabola through
   the log power of the top three bins. This is the minute's Doppler, in Hz
   and in ppb of the nominal (`hz / (MHz × 1e6) × 1e9`).
4. SNR: the peak over the median power 3–20 Hz from the carrier, in dB
   per 1/60 Hz bin.
5. Quality per minute: `weak` (SNR under 10 dB), `edge` (peak beyond
   1.35 Hz, so the carrier may be outside the window), `gaps` (the file is
   under 99 % complete), else `ok`. Only `ok` and `gaps` minutes are used
   for the comparison; the others are drawn as faint dots.

The drill-down keeps WWV at one value per minute (two per WSPR cycle).
Shorter windows would cost resolution: 10 s gives 0.1 Hz bins against
60 s's 1/60 Hz.

**Cost.** The file is streamed through `/usr/bin/zstd -dc` (the browser's
venv has no `zstandard` module, and adding it would mean a new
dependency and a venv rebuild on every station) and decimated 960 000
samples (7.7 MB) at a time, so the 115 MB of a whole file is never held at
once. On a synthetic 10-minute file this took about 0.3 s of CPU (zstd
included) on the development VM; budget up to ~1 s on a station's
Beelink. Peak memory is the Python process plus ~20 MB, and zstd's ~40 MB. At most two files are measured at once (requests and the
background thread together), each zstd runs with `--memory=64MB`, and a
zstd that runs longer than 120 s is killed. Bad data (zstd reports
corruption, a short payload, a malformed sidecar) is cached as a failure
until the file changes; anything else (killed, timed out, unreadable, out
of zstd memory) is logged and retried after 10 minutes. If the cache
itself cannot be read or written, the page degrades and logs, never 500s.

**The cache.** Each file is measured once. Its minutes go into
`/var/cache/wspr-carrier-browser/wwv-carrier.sqlite` (the unit's
`CacheDirectory=`, `$CACHE_DIRECTORY`), keyed by the file's size and
mtime, so a rewritten file is measured again. Six channels make 8640 rows
a day, about 1 MB. Rows are kept for 30 days, so the page shows WWV for
days whose raw buffers are gone. If the directory cannot be written, the
results are kept in memory only (logged).

- A request measures at most two files itself (a drill-down, or the
  newest ten minutes). Concurrent requests for one file share one
  measurement.
- More than that (a cold day) is queued for one background thread, one
  file at a time, at a quarter duty cycle (it sleeps three times as long
  as each file took). The card says how many files are still queued, and
  the page reloads itself every 30 s until none are.
- With `CARRIER_BROWSER_WWV_PREFILL=1` (the default) the same thread keeps
  the current UTC day measured as files arrive: six files every ten
  minutes, a few seconds of CPU.

**WWV and WWVH.** WWVH (Kauai) transmits on 2.5, 5, 10 and 15 MHz too,
on the same frequencies, so on the `SHARED_*` channels the spectrum holds
both stations. Those cards say **WWV+WWVH**. The page measures the
**stronger** peak, which may be WWVH's. If the two Dopplers differ by
less than about 0.05 Hz, they are one blended peak. When the minute's
spectrum resolves a second peak within ±1.5 Hz, no more than 10 dB below
the first, at least 0.05 Hz (3 bins) from it and with a dip of 3 dB or
more between them, both are reported (`hz2`, `ppb2`, `snr2`) and the
minute is marked **two carriers**. The cycle's WWV value is still the
stronger peak. WWV 20 and 25 MHz are WWV only; a second peak there is
multipath.

**The fallback.** If no root has a raw-buffer directory for a
frequency's channel, the old reader answers instead, labelled
**hf-timestd L2 doppler_hz (unreliable)**. The card says so, and no
WW0WWV − WWV difference or median is given against it. Its own ±0.5 Hz
range applies. It reads `L2_tick_timing` (`station = 'WWV'` rows) of
`/var/lib/timestd/phase2/timestd.db`, read-only, and says **hf-timestd
not running** when the WAL files are gone. `--timestd-db none` turns it
off; `--raw-buffer none` uses only it.

The service user (`wsprrec`) must be able to read the raw-buffer
directories and files (and, for the fallback, `timestd.db` with its
`-wal` and `-shm`). Any other source can be plugged in through
`ww0wwv.WwvSource.series()`.

### ppb or Hz

The **Doppler in [ppb] [Hz]** switch in the header changes every Doppler
chart, on the cards and in the drill-down:

- **ppb** (the default) is the comparison: WW0WWV and WWV on one scale.
- **Hz** shows each signal's own offset from its own nominal, the way
  other charts show it:
  - WWV: its carrier's offset from nominal, as measured;
  - WW0WWV: its RF minus its nominal.

  Hz at different carrier frequencies do not compare directly. The alias
  lines become WWV's ±1.5 Hz search window.

The JSON carries both units:

- `/api/compare`: `ppb` and `hz` per cycle and per WWV point (with
  `snr`, `q`, `ok`, `two`, and `hz2`/`ppb2`/`snr2` for a second carrier),
  plus `wwv_ppb`, `wwv_hz`, `wwv_two` and `wwv_unreliable` per cycle, and
  `wwv_blend` (`WWV` or `WWV+WWVH`), `wwv_two_minutes` and the series'
  `source`, `reliable` and `pending` per band;
- `/api/cycle`: `summary.hz`, `hz` on every track window, `wwv_hz`,
  `wwv_two` and `wwv_blend`.

The switch is remembered in the browser.

### Signal levels (dBm)

Each card has a third panel: WW0WWV's signal level in **dBm at the antenna
feed**, one point per cycle, with the noise floor in 2500 Hz as a grey
line. The drill-down shows the same level per track window.

**How it is computed.** wsprd's SNR is the signal over the noise in
2500 Hz. The recorder's noise rows are calibrated **dBm in 1 Hz**: the
`noise.py` port of wsprdaemon's method, the same numbers as wsprdaemon's
noise graphs ("dBm in 1Hz"). So:

```
level_dbm = wsprd SNR + noise_dbm_hz + 10·log10(2500)        (+33.98 dB)
```

The track windows use the carrier's own per-window SNR, measured the same
way, in 2500 Hz.

**Where the noise comes from.** These are the rows the recorder ships to
wsprdaemon.org:

- They live in the sink's queue: `pending_uploads` with `target_db` `wspr`
  (or `$SIGMOND_SQLITE_DB_WSPR`) and `target_table` `noise`.
- Each holds JSON with `time`, `band`, `reporter_id`, `rx_source`,
  `fft_noise_dbm` and `rms_noise_dbm`.
- The FFT value (wsprd's own 1369.5–1630.5 Hz passband) is used, else
  the RMS one. A 0.0 means "not measured".
- A row matches a carrier by receiver directory (the safe reporter id, with
  `_<source>` for multi-source), band and cycle start.

The sink is found as `hamsci_sink` finds it:
`$CARRIER_BROWSER_SINK_DB` / `--sink-db`, `$HAMSCI_SINK_PATH`,
`$SIGMOND_SQLITE_PATH`, `/etc/hs-uploader/sink.toml`,
`/var/lib/sigmond/sink.db`, `/var/lib/hs-uploader/sink.db`. It is opened
`mode=ro`. sigmond trims noise rows after about **24 h**
(`WSPR_RETENTION_MIN`), so older days show no level.

**How solid it is.** The calibration has these parts:

- wsprdaemon's KiwiSDR-derived base constants;
- the per-band KA9Q/RX888 offsets measured at AI6VN against a co-located
  calibrated KiwiSDR (±0.3 dB RMS, ±0.1 dB C2 against it);
- a Kiwi source's `noise_offset_db`.

That makes it a good **relative** calibration between bands and sites.
Absolute accuracy is only as good as that reference KiwiSDR's own (a few
dB). The level adds wsprd's integer SNR (±1 dB, own noise estimate),
which is not exactly the FFT floor's estimator. Read a level as about
±3 dB absolute, much better relative from cycle to cycle.

**WWV has no calibrated level.** The raw IQ is in radiod's uncalibrated
units, and hf-timestd stores no calibrated carrier power either (its L1
`carrier_power_db` is never written). The page says so and does not
invent one. WWV's SNR (peak over median noise) stays on the SNR panel.

### WWV's alias range

WWV's peak is searched only within **±1.5 Hz** of nominal. (The old
tick-phase `doppler_hz` resolved only ±0.5 Hz.) A larger true Doppler
leaves the carrier outside the window, and the strongest thing inside it
(a sidelobe, WWVH, noise) is measured instead. In ppb that is:

| WWV | searched |
|---|---|
| 2.5 MHz | ±600 ppb |
| 5 MHz | ±300 ppb |
| 10 MHz | ±150 ppb |
| 15 MHz | ±100 ppb |
| 20 MHz | ±75 ppb |
| 25 MHz | ±60 ppb |

Each card states the range and draws it as dashed lines. A cycle is
flagged **WWV may be aliased** (a hollow square, no difference given, left
out of the median) when:

- WW0WWV's ppb lies outside WWV's range (same path, so WWV's true Doppler
  is probably outside it too); or
- WWV's value is within 10 % of the edge (beyond 1.35 Hz).

`/api/compare` gives `wwv_limit_ppb` and `wwv_limit_hz` per band (0.5 Hz
when the series is the unreliable fallback), and `wwv_alias` and
`diff_ppb` (null when flagged or unreliable) per cycle.

### All spots

The **All spots** page is the original browser:

- pick a date and a cycle;
- see each band's spots (call, message, SNR, fine frequency, spread);
- click a spot to plot its spectrogram, Doppler and SNR over the
  transmission;
- overlay several spots in ppb;
- download the `.wav`.

With the default `calls = ["WW0WWV"]` it shows only WW0WWV.

### Running it

The `wspr-carrier-browser.service` unit is **installed but not enabled**.
On the station:

```bash
sudo systemctl enable --now wspr-carrier-browser.service
```

It listens on **127.0.0.1:8089** only. From your own machine:

```bash
ssh -L 8089:127.0.0.1:8089 <station>
# then open http://localhost:8089/
```

The unit takes its settings from the environment. Change them with
`sudo systemctl edit wspr-carrier-browser`:

| Variable | Default | Meaning |
|---|---|---|
| `CARRIER_ARCHIVE_ROOT` | `/var/lib/wspr-recorder/carrier-archive` | must match `[carrier_archive] root` |
| `CARRIER_BROWSER_BIND` | `127.0.0.1` | listen address (`--bind`) |
| `CARRIER_BROWSER_PORT` | `8089` | port (`--port`) |
| `CARRIER_BROWSER_ALLOWED_HOSTS` | empty | extra host names the page answers to, comma-separated (`--allow-host`) |
| `TIMESTD_RAW_BUFFER` | `/var/lib/timestd/raw_buffer:/dev/shm/timestd/raw_buffer` | hf-timestd's raw IQ roots, colon-separated, first wins (`--raw-buffer`), or `none` |
| `CARRIER_BROWSER_WWV_PREFILL` | `1` | measure the current UTC day's WWV in the background (`--wwv-prefill`) |
| `CARRIER_BROWSER_WWV_CACHE` | empty (`$CACHE_DIRECTORY`, i.e. `/var/cache/wspr-carrier-browser`) | where the WWV cache lives (`--wwv-cache-dir`) |
| `TIMESTD_DB` | `/var/lib/timestd/phase2/timestd.db` | hf-timestd's database, the unreliable fallback only, or `none` |
| `CARRIER_BROWSER_SINK_DB` | empty (found as hamsci_sink finds it) | the sink with wspr-recorder's noise rows, for dBm levels (`--sink-db`), or `none` |

#### Reaching it through a host relay

A relay can forward a port to the VM, for example the Proxmox host
forwarding to it as it does for station-web on :8000. The browser must
then listen on an address the relay can reach, and the unit's firewall
must let the relay in:

```ini
# systemctl edit wspr-carrier-browser
[Service]
Environment=CARRIER_BROWSER_BIND=0.0.0.0
# the unit has IPAddressDeny=any + IPAddressAllow=localhost; this adds to the allow list
IPAddressAllow=192.0.2.10        # the relay's address (or its subnet)
# only if users reach it by a *name* (an IP address always works):
Environment=CARRIER_BROWSER_ALLOWED_HOSTS=station.example.org
```

### Safety

- **No authentication.** Anyone who can reach the port can read the
  whole archive and the WWV series. Loopback (the default) limits that
  to people with a shell on the host. Bound to `0.0.0.0` or a LAN
  address, it is limited only by the network: keep the relay and the
  unit's `IPAddressAllow=` as narrow as you can, and never expose it to
  the internet. The data is not secret, but the page was not built to
  face hostile traffic.
- **Bounded.** At most 8 requests are served at once (more get 503).
  Concurrent requests for one day's comparison share one computation. The
  per-file cache is LRU-bounded (8000 summaries, 300 tracks: a few MB),
  and the unit sets `MemoryMax=256M` and `CPUQuota=50%` (zstd runs in the
  same cgroup, so it counts too). WWV's raw IQ is measured one file at a
  time, streamed in 7.7 MB chunks. A bad carrier
  file is skipped (logged once), or answered with a JSON error, never a
  dropped connection.
- **Read-only.** It answers only GET. It never writes the archive or
  hf-timestd's files; the hf-timestd database is opened `mode=ro` and the
  raw buffers are only read by `zstd -dc`. The one thing it writes is its
  WWV cache, `/var/cache/wspr-carrier-browser/wwv-carrier.sqlite`. The
  unit runs it with `ProtectSystem=strict`, no `ReadWritePaths=`, and
  only that `CacheDirectory=` writable.
- **Only `--root` is served.** `/file` resolves symlinks and refuses any
  file whose real path is outside the root. The listings skip symlinks,
  so a link cannot reveal another file's name or comment. `date`, `hhmm`
  and `band` are checked against strict patterns.
- **DNS rebinding is blocked whatever the bind.** A request is refused
  (403) unless its `Host` header is:
  - an IP address (the address the user typed; a rebinding attack always
    arrives under the attacker's own host name);
  - `localhost`;
  - this host's name;
  - a name in `CARRIER_BROWSER_ALLOWED_HOSTS`.
- **Escaping.** Text taken from file names and WAV comments is
  HTML-escaped on the page. The CSP allows only the page's own inline
  script and style, and the page loads nothing from other origins.
- The unit also sets `IPAddressDeny=any` and `IPAddressAllow=localhost`,
  so a changed bind does nothing until a drop-in adds the relay's
  address.

## Future work

The following is not built yet.

- **More beacons.** The comparison is WW0WWV against WWV only. The archive
  can already keep other calls (`calls`). Comparing them against WWV, or
  against WWVH and CHU, needs their nominal frequencies and a choice of
  reference per path.
- **WWV inside the minute.** WWV is one value a minute, drawn as a bar
  in the drill-down. A finer track from the same raw IQ (10 s windows,
  0.1 Hz bins, or a phase-slope estimate) would match WW0WWV's 2 s
  windows.
- **Telling WWV from WWVH.** On shared frequencies the stronger peak is
  taken. Their minute schedules (WWVH's 1200 Hz tones and voice slots)
  could identify which station each peak is.
