# KiwiSDR Source (Phase 4c, fixed bands) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** wspr-recorder records and decodes WSPR from a KiwiSDR's channels, one fixed band per channel, through the same BandRecorder → decode → spot/noise/upload path it uses for radiod.

**Architecture:** A new `[[source]]` kind `kiwisdr` builds a `KiwiSourceManager` instead of a `ReceiverManager`. It opens one Kiwi WebSocket audio channel per configured band, resamples the Kiwi's ~12001 Hz audio to exactly 12000 Hz, and feeds `BandRecorder.on_samples()` with a synthetic stream-quality object (a sample counter stands in for RTP). Timing anchors on the host clock via the existing wall-clock correlation path; the row's tier says what the Kiwi actually has. Everything downstream of `BandRecorder` is untouched.

**Tech Stack:** Python 3.11, numpy, scipy.signal (resampling), `websocket-client` (Kiwi WebSocket), pytest. No `kiwiclient` dependency (it is not on PyPI and vendors its own websocket stack); the Kiwi audio protocol subset we need is small and is pinned by recorded-frame tests.

**Spec:** sigmond `docs/superpowers/specs/2026-10-30-multi-receiver-station-design.md` §7b (items 1, 4, 6 here; 2, 3, 5 are the sigmond plumbing plan). Rob 2026-11-22: fixed bands first, proven on AI6VN-2 against the Kiwi at 10.20.23.73:8073; ON5KQ waits for scheduling + a parity run vs wsprdaemon.

## Global Constraints

- Source key format: `kiwisdr:<host>:<port>` (matches sigmond `sources.py`; `wav_writer.source_slug` already strips the `kiwisdr:` prefix).
- WSPR only (§7b title). FT4/FT8/FST4W on Kiwi sources are out of scope: a `kiwisdr` source with a non-WSPR mode in `[[band]]` modes is a config error.
- Bands ≤ channels (§7b.2): more bands than `channels` is a config error, not a silent truncation.
- Never fight web listeners for channels (§7b.6): a refused channel is retried once per 2-minute cycle, never in a tight loop.
- Report "N of M channels obtained" per cycle (§7b.6) in the log, the status/IPC dict, and `inventory`/`validate` JSON.
- Never claim an injected timing tier (§7b.4).
- Kiwi audio request matches wsprdaemon's recorder settings (wsprdaemon `kiwi-utils.sh:483-486`): `mod=usb`, passband 1340–1660 Hz, AGC off with manual gain 60 dB, no compression, user string `wsprdaemon_v<WD version>` replaced by `wspr-recorder_v<version>`.
- Sample rate into BandRecorder: exactly 12000 Hz (resampled; no `SPOT_FREQ_ADJ_HZ` +0.1 Hz fudge on spots).
- Spec rule from 2026-11-18 (sigmond decoder_choice): WAV sample format follows the selected decoders (wspr-recorder 528a3bf `decoders_read_float()`); the Kiwi path writes WAVs through the same writer, so it inherits this unchanged.

## Review Focus

1. Kiwi drops the WebSocket mid-cycle (reboot, network blip) → the band's current 2-minute cycle is marked incomplete (gap recorded), the channel reconnects at the next cycle boundary, and no stale samples are stitched across the gap. Test in Task 4.
2. Kiwi refuses the channel (`too_busy` / all channels in use / `badp` wrong password) → band reported not producible, logged once per cycle, retried next cycle; a wrong password is reported as a configuration fault and NOT retried every cycle. Test in Task 4.
3. Kiwi reports a sample rate far from 12000 (firmware change, 20.25 kHz wideband mode) → refuse the channel with a clear error rather than resample garbage. Test in Task 2.
4. Host clock unsynchronised (chrony not tracking) at startup → same behaviour as radiod sources without authority: wall-clock correlation with the existing warning, tier label says host clock. Test in Task 5.
5. Kiwi `/status` unreachable while audio still flows → overload count reads "unknown" (None), never 0. Test in Task 6.

---

### Task 1: Kiwi source config

**Files:**
- Modify: `wspr_recorder/config.py` (`SourceConfig` ~line 230; `[[source]]` parse ~line 590)
- Test: `tests/test_config_kiwisdr.py`

**Interfaces:**
- Produces: `SourceConfig.kind: str` (`"radiod"` default, or `"kiwisdr"`); for kiwisdr: `SourceConfig.host: str`, `SourceConfig.http_port: int`, `SourceConfig.channels: int`, `SourceConfig.password: str` (default `""`); `key = f"kiwisdr:{host}:{http_port}"`; `status_address` left `""` for kiwisdr. `Config.validate()` returns error strings for the rules below.

- [ ] **Step 1: Write the failing tests**

```python
def test_kiwisdr_source_parses():
    cfg = load_config_from_text('''
[[source]]
kiwisdr = "10.20.23.73:8073"
channels = 8
[[band]]
frequency = 14095600
modes = ["W2"]
''')
    s = cfg.sources[0]
    assert (s.kind, s.host, s.http_port, s.channels) == ("kiwisdr", "10.20.23.73", 8073, 8)
    assert s.key == "kiwisdr:10.20.23.73:8073"

def test_kiwisdr_default_port_8073():
    s = load_config_from_text('[[source]]\nkiwisdr = "kiwi.local"\nchannels = 4\n').sources[0]
    assert s.http_port == 8073

def test_kiwisdr_more_bands_than_channels_is_error():
    cfg = load_config_from_text(kiwi_cfg(channels=2, band_freqs=[7038600, 10138700, 14095600]))
    assert any("3 bands > 2 channels" in e for e in cfg.validate())

def test_kiwisdr_non_wspr_mode_is_error():
    cfg = load_config_from_text(kiwi_cfg(channels=4, band_freqs=[14074000], modes=["FT8"]))
    assert any("WSPR only" in e for e in cfg.validate())

def test_radiod_source_unchanged():
    s = load_config_from_text('[[source]]\nstatus = "hf.local"\n').sources[0]
    assert (s.kind, s.key) == ("radiod", "radiod:hf.local")
```

(`load_config_from_text` / `kiwi_cfg` are small helpers in the test file that write a temp TOML and call the existing loader.)

- [ ] **Step 2: Run** `pytest tests/test_config_kiwisdr.py -v` — Expected: FAIL (no `kind` attribute).
- [ ] **Step 3: Implement** in `config.py`: a `[[source]]` entry with a `kiwisdr` key parses as above (port split on the last `:`); `channels` required for kiwisdr (missing → validate error "kiwisdr source needs channels"); validate rules as tested, counted per kiwisdr source against `Config.bands`. WSPR modes are the `DecodeMode`s whose name starts with `W` or `F` (FST4W): reuse the existing mode table, don't hard-code strings.
- [ ] **Step 4: Run** the new tests and `pytest tests/test_config.py` — Expected: all PASS.
- [ ] **Step 5: Commit** `feat(config): kiwisdr [[source]] kind (Phase 4c)`.

### Task 2: Kiwi WebSocket audio channel

**Files:**
- Create: `wspr_recorder/kiwi_channel.py`
- Create: `tests/fixtures/kiwi/` (recorded frames, see Step 1)
- Test: `tests/test_kiwi_channel.py`
- Modify: `pyproject.toml` (add `websocket-client>=1.6`)

**Interfaces:**
- Produces: `class KiwiChannel` with
  `__init__(self, host: str, port: int, freq_hz: int, *, password: str = "", user: str, on_audio: Callable[[np.ndarray, float], None], on_state: Callable[[str, str], None])`,
  `start() -> None` (background thread), `stop() -> None`, property `sample_rate: Optional[float]` (Kiwi-reported, e.g. 12001.135).
  `on_audio(samples_int16_as_float32, rx_monotonic)` per SND frame; `on_state(state, detail)` with state in `{"connected", "refused", "auth_failed", "dropped", "bad_rate"}`.
- Protocol subset (to pin in tests): open `ws://host:port/<epoch_ms>/SND`; send `SET auth t=kiwi p=<password>`; wait for `MSG ... sample_rate=<float>` and `MSG audio_init=…`; send `SET mod=usb low_cut=1340 high_cut=1660 freq=<dial_khz>` where dial = band frequency the same way wspr-recorder tunes radiod (USB dial = WSPR dial frequency), `SET agc=0 hang=0 thresh=-100 slope=6 decay=1000 manGain=60`, `SET compression=0`, `SET ident_user=<user>`, `SET AR OK in=12000 out=44100`, then `SET keepalive` every 5 s. Audio arrives as binary `SND` frames: 3-byte tag, 1 byte flags, 4-byte LE sequence, 2-byte BE S-meter, then big-endian int16 PCM. Refusals arrive as `MSG too_busy=…` / `MSG badp=1`.

- [ ] **Step 1: Record fixtures.** From AI6VN-2 against 10.20.23.73:8073 (read-only, one channel, 30 s): save the server text messages and the first 50 binary frames to `tests/fixtures/kiwi/handshake.jsonl` and `snd_frames.bin` with a small capture script `scripts/kiwi_capture.py` (commit the script). If the Kiwi is unreachable, stop and report — do not invent frames.
- [ ] **Step 2: Write the failing tests** (replay fixtures through a fake socket):

```python
def test_parses_sample_rate_from_handshake(): ...  # ch.sample_rate == pytest.approx(<value in fixture>, abs=0.01)
def test_snd_frame_to_float32_int16_scale(): ...    # first frame decodes to n samples, dtype float32, values == int16/32768
def test_sends_wd_equivalent_settings(): ...        # sent text contains "mod=usb low_cut=1340 high_cut=1660", "manGain=60", "compression=0"
def test_too_busy_reports_refused(): ...            # on_state called with ("refused", ...), no audio
def test_badp_reports_auth_failed(): ...
def test_rate_outside_11990_12010_reports_bad_rate(): ...  # 20250.0 → ("bad_rate", "20250.0")
def test_sequence_gap_fills_zeros(): ...            # skipped seq number → on_audio receives zeros of the missing frame's length (the connection stays up); Task 4 accounts the gap
```

- [ ] **Step 3: Run** — Expected: FAIL (module missing).
- [ ] **Step 4: Implement** `KiwiChannel` with `websocket.WebSocketApp` on its own thread; frame sizes and the int16 endianness come from the fixture, not assumptions.
- [ ] **Step 5: Run** `pytest tests/test_kiwi_channel.py -v` — Expected: PASS.
- [ ] **Step 6: Commit** `feat(kiwi): WebSocket audio channel with WD-equivalent settings`.

### Task 3: Resample to 12000 Hz

**Files:**
- Create: `wspr_recorder/kiwi_resample.py`
- Test: `tests/test_kiwi_resample.py`

**Interfaces:**
- Produces: `class StreamResampler(in_rate: float, out_rate: int = 12000)` with `process(x: np.ndarray) -> np.ndarray` (stateful across calls; float32 in/out) and `set_in_rate(in_rate: float)`.

- [ ] **Step 1: Failing tests**

```python
def test_1500hz_tone_stays_1500hz():
    r = StreamResampler(12001.135)
    t = np.arange(120 * 12001) / 12001.135
    y = np.concatenate([r.process(c) for c in np.array_split(np.sin(2*np.pi*1500*t).astype(np.float32), 977)])
    f = np.fft.rfftfreq(len(y), 1/12000)[np.argmax(np.abs(np.fft.rfft(y)))]
    assert abs(f - 1500.0) < 0.01          # < 0.01 Hz: well inside WSPR's 1.46 Hz bin
def test_output_count_tracks_rate():
    r = StreamResampler(12001.135); n_in = 12001 * 120
    n_out = sum(len(r.process(c)) for c in np.array_split(np.zeros(n_in, np.float32), 300))
    assert abs(n_out - round(n_in * 12000 / 12001.135)) <= 2
def test_chunking_invariant(): ...   # same input in 1 chunk vs 1000 chunks → max abs diff < 1e-4
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** with a polyphase/linear-phase approach that carries state between chunks (e.g. `scipy.signal.resample_poly` is not stateful — use a fractional-delay FIR or keep an overlap tail; the chunking test decides). **Step 4: Run** — PASS. **Step 5: Commit** `feat(kiwi): stateful resampler to exactly 12000 Hz`.

### Task 4: KiwiSourceManager (channels → BandRecorders)

**Files:**
- Create: `wspr_recorder/kiwi_source.py`
- Test: `tests/test_kiwi_source.py`

**Interfaces:**
- Consumes: `SourceConfig` (Task 1), `KiwiChannel` (Task 2), `StreamResampler` (Task 3), `BandRecorder.on_samples(samples, quality)`.
- Produces: `class KiwiSourceManager` with the ReceiverManager surface `__main__` uses:
  `__init__(config, sink_factory, source: SourceConfig, on_band_producible)`, `connect() -> bool`, `start_streams() -> None`, `stop_streams() -> None`, `check_health() -> bool`, `get_status() -> dict` (includes `"channels_obtained": int`, `"channels_wanted": int`), `read_frontend_ad_over(ssrc) -> Optional[int]`, `shutdown() -> None`, `state.connected: bool`.
  `class KiwiStreamQuality` (dataclass) with the attributes `BandRecorder.on_samples` reads: `first_rtp_timestamp: int = 0`, `total_samples_delivered: int`, `batch_gaps: list`, `sample_rate: int = 12000`, other `sq_*` counters default 0.
  Synthetic SSRC per band = the band's frequency in Hz (stable, unique within the source).

- [ ] **Step 1: Failing tests** (fake `KiwiChannel` injected):

```python
def test_one_channel_per_band_and_status_counts(): ...  # 3 bands, 1 refused → get_status() == {..., "channels_obtained": 2, "channels_wanted": 3}
def test_refused_band_not_producible_and_retried_next_cycle(): ...  # on_band_producible(band, False); exactly one retry per 120 s of fake time
def test_auth_failed_not_retried(): ...   # badp → producible False, error logged once, no retry
def test_samples_reach_band_recorder_at_12000(): ...  # 12001.135 Hz in → recorder.on_samples total ≈ 12000*s; quality.total_samples_delivered monotonic
def test_drop_records_gap_and_reconnects_at_cycle_boundary(): ...  # dropped mid-cycle → quality.batch_gaps has a gap of the missing length; reconnect waits for next even minute
def test_cycle_log_line_n_of_m(caplog): ...  # "kiwisdr:10.20.23.73:8073: 2 of 3 channels obtained"
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** Gap length on a drop = wall-clock seconds missing × 12000, capped at `BandRecorder.max_gap_samples` (BandRecorder already caps; pass the true length). **Step 4: Run** — PASS. **Step 5: Commit** `feat(kiwi): KiwiSourceManager feeding BandRecorder`.

### Task 5: Wire kiwisdr sources into the recorder + timing tier

**Files:**
- Modify: `wspr_recorder/__main__.py` (source loop ~line 2175; `_build_sink` ~line 316)
- Modify: `wspr_recorder/band_recorder.py` only if needed to accept a tier override (see Step 3)
- Test: `tests/test_kiwi_wiring.py`

**Interfaces:**
- Consumes: `KiwiSourceManager` (Task 4), `timing_service.create_sync_strategy(sample_rate, channel_info=None)`.
- Produces: `self.receiver_managers[src.key]` is a `KiwiSourceManager` for kiwisdr sources; `_build_sink` passes `channel_info=None` for them.

- [ ] **Step 1: Failing tests**

```python
def test_kiwisdr_source_builds_kiwi_manager(): ...   # config with one kiwisdr + one radiod source → types per key
def test_kiwi_band_uses_wallclock_correlation(): ...  # sync strategy has channel_info None; first sync logs "correlated via wall clock"
def test_kiwi_rows_tier_is_not_injected(): ...        # spot/noise rows from a kiwi band carry timing tier "host-clock" (or the tier name the existing no-authority path uses) — never T5/T6
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement:** branch on `src.kind`; for the tier, use the label the existing no-authority radiod path already emits (read it from `timing_service`; do not invent a new tier name). The Kiwi's `/gps` fix is recorded as a separate status field (`kiwi_gps_fix: bool`) in `get_status()`, not as a tier — spec §7b.4 ("never claims an injected tier"); host-clock anchoring is what actually times the audio in USB mode. **Ruling to confirm with Michael:** if a later phase uses the Kiwi's IQ-mode GPS timestamps for anchoring, that is when a Kiwi-GPS tier appears.
- [ ] **Step 4: Run** the new tests plus `pytest tests/test_sync_strategy.py tests/test_timing_guard.py` — PASS. **Step 5: Commit** `feat(kiwi): wire kiwisdr sources into the recorder`.

### Task 6: ADC overload and noise calibration

**Files:**
- Create: `wspr_recorder/kiwi_status.py`
- Modify: `wspr_recorder/kiwi_source.py` (`read_frontend_ad_over`)
- Test: `tests/test_kiwi_status.py`

**Interfaces:**
- Produces: `fetch_kiwi_status(host: str, port: int, timeout: float = 5.0) -> Optional[dict]` (parses `http://host:port/status`, `key=value` lines; returns None on any error); `KiwiSourceManager.read_frontend_ad_over(ssrc) -> Optional[int]` = monotonic `adc_ov` from `/status`, None when unreachable.
- Noise: `[[source]]` kiwisdr gets `noise_offset_db: float`, default `0.0` until the parity run (Task 7) measures it against wsprdaemon on the same Kiwi; applied where the noise row's level is computed, only for kiwi bands.

- [ ] **Step 1: Failing tests**

```python
def test_parses_adc_ov_from_status_fixture(): ...    # tests/fixtures/kiwi/status.txt (captured in Task 2 Step 1) → adc_ov int
def test_status_unreachable_returns_none(): ...      # connection refused → None
def test_ad_over_none_not_zero_when_unreachable(): ...
def test_noise_offset_applied_to_kiwi_rows_only(): ...
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (poll at most once per cycle per source; cache). **Step 4: Run** — PASS. **Step 5: Commit** `feat(kiwi): adc_ov overloads and per-source noise offset`.

### Task 7: Live proof on AI6VN-2 (no code unless it fails)

**Files:**
- Create: `docs/KIWISDR.md` (operator note: config example, what "N of M" means, known limits: fixed bands, WSPR only, host-clock timing)
- Modify: `config.toml.example` (commented kiwisdr `[[source]]` example)

- [ ] **Step 1:** On AI6VN-2 run a test instance (not the station's production instance) with one kiwisdr source for 10.20.23.73:8073, 2–4 WSPR bands, uploads OFF. Announce on the claude-bus before starting (AI6VN-2 is shared with Michael's work).
- [ ] **Step 2:** Run 2 hours. Expected: every cycle logs "N of N channels obtained"; WAVs 100 % complete; spots decode; dt within ±1 s of the same Kiwi's wsprdaemon spots on wsprnet for overlapping cycles (if WD is still feeding that Kiwi), frequency within ±1 Hz.
- [ ] **Step 3:** Record the measured noise offset vs wsprdaemon (if available) or vs a co-located radiod band; set it as the documented default for that Kiwi model in `docs/KIWISDR.md` (keep the code default 0.0).
- [ ] **Step 4:** Commit docs. Report results on the bus; the sigmond plumbing plan (§7b.2–3, 5) starts after this.

---

**Out of scope (separate plans):** sigmond site-profile `channels`/`bands` parsing, bring-up Stage 3b, `smd check` "Kiwi unreachable", instance creation (§7b.2, 3, 5); band scheduling / sunrise-sunset rotation; IQ-mode GPS-timestamp anchoring; non-WSPR modes on Kiwis.
