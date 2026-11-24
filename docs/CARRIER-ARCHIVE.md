# Carrier archive

> **Audience:** operator/contributor
> **Status:** new 2026-11-24, off by default, not yet run on a live station

An optional archive of every WSPR / FST4W spot's **carrier**, plus a small
read-only web page to browse it. Both are **disabled by default**.

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

## Disk use

A carrier file holds 15 samples/s × 8 bytes for the length of the
transmission:

| Mode | Per spot |
|---|---|
| W2, F2 | ≈ 13–15 KB |
| F5 | ≈ 36 KB |
| F15 | ≈ 108 KB |
| F30 | ≈ 216 KB |

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

`wspr_recorder/carrier_browser.py` (`python -m wspr_recorder.carrier_browser
--root DIR [--bind 127.0.0.1] [--port 8089]`) serves a page with these
controls:

- pick a date and a cycle
- see each band's spots (call, message, SNR, fine frequency, spread)
- click a spot to plot its spectrogram, Doppler and SNR over the
  transmission
- overlay several spots in ppb
- download the `.wav`

It uses only the Python standard library.

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

If you changed `root` in the recorder config, set the same path in the
browser unit: `sudo systemctl edit wspr-carrier-browser` and add
`Environment=CARRIER_ARCHIVE_ROOT=/your/root`.

### Safety

- The browser is read-only. It handles only GET requests and never
  creates or writes anything. The unit runs it with `ProtectSystem=strict`
  and no writable paths.
- It serves only what is under `--root`. `/file` resolves symlinks and
  refuses any file whose real path is outside the root. The listings skip
  symlinks, so a link cannot reveal another file's name or comment.
  `date` and `hhmm` must be digits.
- It binds to 127.0.0.1 by default. While bound to loopback it refuses any
  request whose `Host` header is not a loopback name, which blocks DNS
  rebinding. The unit also sets `IPAddressDeny=any` and
  `IPAddressAllow=localhost`.
- Text taken from file names and WAV comments is HTML-escaped on the page.
- It has **no authentication**. Do not bind it to a LAN or public
  address.

## Future work

The following is not built yet.

- **WWV Doppler side by side.** Show a WSPR/FST4W carrier's Doppler next to
  the WWV/WWVH/CHU Doppler that hf-timestd measures at the same time on a
  nearby frequency. This would separate the ionosphere's effect from the
  transmitter's own drift.
