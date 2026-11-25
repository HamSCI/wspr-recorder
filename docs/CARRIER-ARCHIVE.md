# Carrier archive

> **Audience:** operator/contributor
> **Status:** new 2026-11-24, off by default, not yet run on a live station; WW0WWV-only beta 2026-11-25

An optional archive of WSPR / FST4W spots' **carriers**, plus a small
read-only web page to browse it. Both are **disabled by default**.

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

The ubersdr decoders at **5eeeb53 or later** (madpsy ubersdr_wsprd PR #6),
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
python -m wspr_recorder.carrier_browser --root DIR [--bind ADDR] [--port 8089] [--timestd-db PATH]
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
  file's JSON, plus that peak. The blue line is WWV, one value per minute
  from hf-timestd. The x axis is UTC, with PST underneath.
- **SNR over the day.** WW0WWV's is wsprd's (dB in 2500 Hz). WWV's is
  hf-timestd's per-tick matched-filter SNR. The two are different
  measures, so compare their trends, not their levels.
- **A summary line:** the cycle count, where WWV's data came from (or
  **no WWV data** and why), and the median WW0WWV − WWV difference.

**Click a WW0WWV point** to open that cycle under the card:

- WW0WWV's carrier spectrogram (±1.5 Hz around the nominal);
- its Doppler track (10 s windows every 2 s, or 30 s windows when wsprd's
  SNR is −15 dB or less), with WWV's minute values drawn as bars over the
  same two minutes;
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

hf-timestd writes one row per channel, station and UTC minute into table
`L2_tick_timing` of its SQLite database, `/var/lib/timestd/phase2/timestd.db`
(hf-timestd's `[storage] sqlite_path`). The page reads these columns:

- `minute_boundary_utc` (epoch);
- `channel` (`WWV_20000`, `SHARED_10000`, …);
- `station` (only `WWV` rows are used; `WWVH` on shared channels is
  skipped);
- `doppler_hz` (the slope across the minute of the phase of WWV's
  ticks, mixed down from the channel's IQ; hf-timestd resolves it within
  ±0.5 Hz);
- `mean_snr_db`.

WWV's ppb is `doppler_hz / (MHz × 1e6) × 1e9`. The page looks for channels
`WWV_<kHz>` and `SHARED_<kHz>`. It opens the database read-only
(`mode=ro`), once per query, and caches each answer for 60 s.

The page never demodulates raw WWV IQ. If the database is missing, or has
no rows for that frequency and day, the card says **no WWV data** with the
reason, and WW0WWV is still plotted. Set `--timestd-db` (or `TIMESTD_DB`)
to another path, or to `none` to turn WWV off. Any other source can be
plugged in through `ww0wwv.WwvSource.series()`.

The service user (`wsprrec`) must be able to read `timestd.db`, and its
`-wal` and `-shm` files.

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
| `TIMESTD_DB` | `/var/lib/timestd/phase2/timestd.db` | hf-timestd's database, or `none` |

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
- **Read-only.** It answers only GET, and never creates or writes
  anything. The hf-timestd database is opened `mode=ro`. The unit runs it
  with `ProtectSystem=strict` and no writable paths.
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
- **WWV inside the minute.** hf-timestd publishes one Doppler a minute,
  and the drill-down draws it as a bar. A finer WWV track (from
  hf-timestd's per-second `L2_tick_phase` carrier phase) would match
  WW0WWV's 2 s windows.
