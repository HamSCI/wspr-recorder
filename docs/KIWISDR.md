# KiwiSDR sources

A `[[source]]` with `kiwisdr = "host[:port]"` makes wspr-recorder record
from a KiwiSDR instead of a radiod. The bands are fixed: each `[[band]]`
gets its own Kiwi audio channel (a WebSocket), and WSPR and FST4W are
decoded from the same 12 kHz audio. FT8 and FT4 are not available on a Kiwi,
because they need frequencies a WSPR-tuned channel does not cover; config
validation rejects them.

## Configuration

```toml
[[source]]
kiwisdr = "10.22.23.70:8073"
channels = 4
password = ""
label = "lab Kiwi"
# key = "kiwisdr:10.22.23.70:8073"
# noise_offset_db = 0.0
```

| Key | Meaning |
|---|---|
| `kiwisdr` | `host` or `host:port`; port defaults to 8073. Selects the Kiwi source kind. |
| `channels` | Required, a positive integer: the audio channels this source may use. Config is rejected if there are more `[[band]]` entries than `channels`. |
| `password` | Optional Kiwi user password; empty by default. |
| `label` | Free-text label. |
| `key` | Optional. `key=` is accepted; the default is `kiwisdr:<host>:<port>`. It names the source in logs, status and `rx_source`. |
| `noise_offset_db` | dB added to the noise row's levels; default 0.0 (see Known limits). |

## "N of M channels obtained"

Every cycle the source logs `kiwisdr:<host>:<port>: N of M channels obtained`
(`get_status()` carries the same two numbers). M is the number of bands; N is
how many Kiwi channels are connected. A band without a channel is skipped, and
the others keep recording.

The Kiwi may refuse a channel:

* `too_busy` (also `down`, `redirect`): the Kiwi has no free channel for us.
  Web listeners are never pre-empted. The band is retried each cycle.
* `badp`: a refusal code. **Only `badp=1` is a password fault.** It is logged
  as a configuration error and retried hourly. Every other code (for example
  `badp=5`, multiple connections from the same IP) is retried each cycle.

## Kiwi admin limit on API connections

The Kiwi admin page limits non-browser (API) connections (`ext_api`).
wspr-recorder's channels are API connections. The lab Kiwi allowed only 4, so
the bands you configure must fit within that limit; setting `channels` higher
does not raise it. Check the Kiwi's admin settings if you get fewer channels
than bands.

## Timing

Timing is the host clock, correlated when each channel connects; no GPS or
hf-timestd authority is used. WAV sidecars say host-clock only. The Kiwi
reports its own sample rate (about 12000 Hz): audio is resampled to exactly
12000 Hz, or passed through when the rate is within 0.001 Hz of 12000.

## Reconnects

A dropped channel (or one silent for 10 s) loses the cycle in progress. The
reconnect is attempted about 10 s before the next even minute, so the next
cycle records whole. The outage is not charged to that next cycle: it is
logged and counted in `get_status()` (`outage_count`, `outage_missing_s` per
channel).

## Known limits

* No band scheduling yet. That comes next; a band change will retune the open
  channel at the even minute rather than reconnect.
* No IQ-mode GPS timestamps; timing is the host clock.
* The noise calibration offset is 0.0 until a parity run with a real antenna.

## Lab results, 2026-11-22

AI6VN-2 with the lab Kiwi (KiwiSDR 1 v1.902), no antenna, manual gain 60,
21:57:30-23:58:10Z:

* 54-57 cycles per band.
* 181 WAV headers, all 12000 Hz x 1,440,000 frames.
* Cycle completion within -0.212..+0.090 s of the boundary over 2 h: no
  measurable drift.
* 0 spots, as expected with no antenna; `adc_ov` = 0.
* A forced disconnect at 22:41:00Z on 20 m reconnected at 22:41:50; only the
  22:40 cycle was lost.
* Unplanned, 23:15:17Z: the Kiwi closed all 4 connections (no reboot). 40 m
  and 30 m were back the next cycle; 80 m at 23:18 and 20 m at 23:20, after
  "no handshake within 10 s" retries.

### No-antenna noise baseline

| Band | rms dBm | fft dBm |
|---|---|---|
| 80 m | -102.8 | -102.0 |
| 40 m | -103.3 | -101.9 |
| 30 m | -102.2 | -101.0 |
| 20 m | -100.7 | -99.7 |
