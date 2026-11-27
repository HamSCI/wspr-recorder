"""
Decoder invocation for wspr-recorder.

Runs wsprd and jt9 on WAV files produced by the ring buffer, collects
raw spot data, and integrates with CallsignDB for type-3 hash resolution.

wsprd runs twice (standard + spreading variant), results merged.
jt9 runs with -Y flag to expose numeric 22-bit hashes for resolution.
"""

import logging
import os
import re
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .callsign_db import CallsignDB
from .carrier_archive import CarrierArchive
from .decode_health import HEALTH
from .decode_mode import DecodeMode, DECODE_MODE_PKT_MODES

logger = logging.getLogger(__name__)

# Maximum ALL_WSPR.TXT size before truncation (matches v3)
MAX_ALL_WSPR_SIZE = 200_000

# Wall-clock kill for a decoder.  WSPRD_TIMEOUT_S applies to the 2-minute
# modes, which really are racing the next cycle; LONG_DECODE_TIMEOUT_S to
# F5/F15/F30, which are not (see decode_fst4w and decode_health.py).
WSPRD_TIMEOUT_S = 110
LONG_DECODE_TIMEOUT_S = 300


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default

# ── which WAV formats the installed decoders can read ──────────────────
#
# sigmond installs two decoder sets side by side under
# /usr/local/libexec/sigmond/decoders/{ubersdr,wsjtx}/ and points
# /usr/local/bin/{wsprd,jt9} at one of them (sigmond bin/smd
# _select_decoders).  Only the UberSDR build reads IEEE-float WAVs.  Stock
# WSJT-X wsprd skips a fixed 44-byte header and reads the payload as int16,
# and jt9 reads int16 too: handed a float WAV they exit 0 with no spots.  A
# station that fell back to the wsjtx set (parity failure, e.g. Zen 4) went
# silent that way — WWV-2, 2026-11-16.  So float is opt-in by evidence: a
# decoder reads float only if it resolves into the ubersdr set; anything
# else (the wsjtx set, a Debian wsprd, a binary we cannot find) gets int16.
_FLOAT_DECODER_SET = "/decoders/ubersdr/"


class DecoderInputError(RuntimeError):
    """A decoder was handed a WAV in a format it cannot read."""


def decoder_reads_float(path: str) -> bool:
    """True iff the decoder at `path` (a name on PATH or a path) is the
    UberSDR build, which reads IEEE-float WAVs."""
    found = shutil.which(path)
    if not found:
        return False
    return _FLOAT_DECODER_SET in os.path.realpath(found)


def decoders_read_float() -> bool:
    """True iff BOTH wsprd and jt9 on PATH read IEEE-float WAVs.  Evaluated
    per call so a `smd` decoder-set swap (a symlink flip, no restart) is
    picked up at the next period."""
    return decoder_reads_float("wsprd") and decoder_reads_float("jt9")


def wav_format_tag(path: Path) -> Optional[int]:
    """The WAVE fmt-chunk format tag (1 = PCM, 3 = IEEE float), or None if
    the file has no readable fmt chunk."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return None
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos = 12
    while pos + 10 <= len(head):
        cid = head[pos:pos + 4]
        size = struct.unpack("<I", head[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            return struct.unpack("<H", head[pos + 8:pos + 10])[0]
        pos += 8 + size + (size & 1)
    return None


# ── fine frequency (ubersdr 262b4db, madpsy PR #6) ─────────────────────
#
# `wsprd -F` prints ALL_WSPR.TXT's frequency %13.9f (WSJT-X's %11.7f) and
# `jt9 --fst4w --fine-freq` fst4_decodes.dat's fsig f9.3 (WSJT-X's f7.1):
# each decode's frequency measured to 0.001 Hz, no column moved.  Only
# ubersdr builds since then accept the options, and a decoder handed one it
# does not know refuses to run, so ask each binary once, as wsprdaemon's
# decoder_fine_freq_flag does: wsprd's usage (printed when it is run with no
# arguments) lists "-F fine", `jt9 --help` lists "--fine-freq".  Cached per
# (path, mtime), so a decoder-set swap is seen at the next decode.
#
# The carrier archive's options (ubersdr e8c2c52, madpsy PR #8) are probed
# the same way: wsprd's usage lists "-I dir:", `jt9 --help` "--carrier-dir".
_WSPRD_FINE_RE = re.compile(r"^ *-F fine", re.MULTILINE)
_WSPRD_CARRIER_RE = re.compile(r"^ *-I dir", re.MULTILINE)
_FINE_FREQ_PROBE_TIMEOUT_S = 10
# (path, help args, resolved path, mtime, option) -> bool.  The option is in
# the key: wsprd's -F and -I are both read from the same bare usage text.
_FINE_FREQ_CACHE: dict = {}


def _help_text(cmd: List[str]) -> Optional[str]:
    """stdout+stderr of a decoder's help run, or None if it cannot run."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL,
                           timeout=_FINE_FREQ_PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout or "") + (r.stderr or "")


def _option_probe(path: str, help_args: List[str], option: str,
                  matches) -> bool:
    """True iff the decoder at `path` lists `option` in its help text.
    Asked once per (path, mtime, option); False if it cannot run."""
    found = shutil.which(path) or path
    try:
        mtime = os.path.getmtime(found)
    except OSError:
        mtime = None
    key = (path, tuple(help_args), found, mtime, option)
    if key not in _FINE_FREQ_CACHE:
        text = _help_text([path, *help_args])
        _FINE_FREQ_CACHE[key] = bool(text is not None and matches(text))
        logger.info("decoder %s option %s: %s", path, option,
                    "yes" if _FINE_FREQ_CACHE[key] else "no")
    return _FINE_FREQ_CACHE[key]


def wsprd_has_fine_freq(path: str) -> bool:
    """True iff the wsprd at `path` takes -F (frequency to 0.001 Hz)."""
    return _option_probe(path, [], "-F",
                         lambda t: bool(_WSPRD_FINE_RE.search(t)))


def jt9_has_fine_freq(path: str) -> bool:
    """True iff the jt9 at `path` takes --fine-freq (FST4W, 0.001 Hz)."""
    return _option_probe(path, ["--help"], "--fine-freq",
                         lambda t: "--fine-freq" in t)


def wsprd_has_carrier_dir(path: str) -> bool:
    """True iff the wsprd at `path` takes -I DIR (each decode's carrier)."""
    return _option_probe(path, [], "-I",
                         lambda t: bool(_WSPRD_CARRIER_RE.search(t)))


def jt9_has_carrier_dir(path: str) -> bool:
    """True iff the jt9 at `path` takes --carrier-dir DIR (FST4W carriers)."""
    return _option_probe(path, ["--help"], "--carrier-dir",
                         lambda t: "--carrier-dir" in t)


# Regex for jt9 -Y numeric hash: <NNNNNNN> (7-digit zero-padded)
_HASH22_RE = re.compile(r'<(\d{1,7})>')
# fst4_decodes.dat leading nutc column: i6.6 → exactly six digits.
_NUTC_RE = re.compile(r"\d{6}")


@dataclass
class RawSpot:
    """One decoded spot in ALL_WSPR.TXT-compatible format."""
    date: str               # YYMMDD
    time: str               # HHMM
    snr: int                # dB
    dt: float               # seconds
    freq: float             # MHz, 6-7 decimals
    call: str               # TX callsign
    grid: str               # TX grid (or "none" / empty)
    power: int              # dBm
    drift: int = 0
    sync_quality: float = 0.0
    ipass: int = 0
    blocksize: int = 0
    jitter: int = 0
    decodetype: int = 0
    nhardmin: int = 0
    cycles: int = 0
    metric: float = 0.0
    pkt_mode: int = 2       # wsprdaemon code: 2=W2, 3=F2, 6=F5, 16=F15, 31=F30
    spreading: Optional[float] = None
    hash22: Optional[int] = None  # numeric hash if unresolved type-3


class DecoderRunner:
    """
    Manages decoder invocation for one band.

    Work directory is stable per band — ALL_WSPR.TXT and hash tables persist
    across decode cycles. CallsignDB is shared across all bands.
    """

    def __init__(
        self,
        band_name: str,
        frequency_hz: int,
        work_dir: Path,
        callsign_db: CallsignDB,
        wsprd_path: str = "wsprd",
        wsprd_spread_path: Optional[str] = None,
        jt9_path: str = "jt9",
        carrier_archive: Optional[CarrierArchive] = None,
    ):
        self.band_name = band_name
        self.frequency_hz = frequency_hz
        self.freq_mhz = frequency_hz / 1e6
        self.work_dir = work_dir
        self.callsign_db = callsign_db
        self.wsprd_path = wsprd_path
        self.wsprd_spread_path = wsprd_spread_path
        self.jt9_path = jt9_path
        # None: no carrier archive, and the decoder command lines are
        # exactly what they were before it existed.
        self.carrier_archive = carrier_archive

        # Ensure work directory exists
        self.work_dir.mkdir(parents=True, exist_ok=True)

        # Track fst4_decodes.dat line count for diffing
        self._fst4_prev_lines: int = 0

    def _check_input(self, wav_path: Path, decoder: str) -> None:
        """Refuse, loudly, to hand `decoder` a WAV it cannot read.

        Stock WSJT-X wsprd/jt9 given an IEEE-float WAV do not error: they
        read the floats as int16 and report no spots, which is
        indistinguishable from a quiet band.  Raise instead, so the cycle
        is logged as a failed decode rather than an empty one."""
        if wav_format_tag(wav_path) == 3 and not decoder_reads_float(decoder):
            msg = (f"{self.band_name}: {decoder} "
                   f"({os.path.realpath(shutil.which(decoder) or decoder)}) "
                   f"cannot read IEEE-float WAV {Path(wav_path).name} — only "
                   f"the ubersdr decoder set reads float; this cycle is NOT "
                   f"decoded.  Write int16 (recorder sample_format) or select "
                   f"the ubersdr decoders.")
            logger.error(msg)
            raise DecoderInputError(msg)

    def decode_wspr(self, wav_path: Path,
                    cycle_start: Optional[float] = None) -> List[RawSpot]:
        """Decode a WSPR-2 WAV (see ``_decode_wspr``) and, when the carrier
        archive is on, file each decode's carrier afterwards."""
        self._check_input(wav_path, self.wsprd_path)
        p1 = self._carrier_scratch(self.wsprd_path, wsprd_has_carrier_dir, "p1")
        p2 = (self._carrier_scratch(self.wsprd_spread_path,
                                    wsprd_has_carrier_dir, "p2")
              if self.wsprd_spread_path else None)
        try:
            return self._decode_wspr(wav_path, cycle_start, p1, p2)
        finally:
            # p2 first: the -n (spreading) pass's carrier wins, as its spot
            # does in _merge_wspr_passes.
            self._file_carriers([p2, p1], "W2", wav_path, cycle_start)

    def _decode_wspr(self, wav_path: Path, cycle_start: Optional[float],
                     carrier_p1: Optional[Path] = None,
                     carrier_p2: Optional[Path] = None) -> List[RawSpot]:
        """
        Run wsprd twice (standard + spreading), merge via best-spot logic.

        1. Pre-populate hashtable.txt from CallsignDB
        2. Run wsprd standard pass
        3. Run wsprd.spreading pass
        4. Merge: for each TX call, prefer spot with spreading data, then best SNR
        5. Ingest new callsigns into CallsignDB

        Returns merged spots with pkt_mode=2 (WSPR-2).
        """
        # Per operator decision 2026-05-19: don't feed CallsignDB into
        # wsprd's hashtable.  wsprd maintains its own per-band
        # hashtable.txt across invocations (the working-dir state file
        # persists between cycles within a tmpfs lifetime).  The
        # CallsignDB-driven write may have been miss-triggering
        # Type-3 hash mappings — observed empirically as ~20%
        # wsprnet rejection rate even with a freshly-zeroed CallsignDB.
        # Leaving wsprd to manage its own table, and only feeding
        # back from wsprd's output via ``ingest_wsprd_hashtable`` so
        # CallsignDB remains useful for jt9 -Y hash resolution.
        # Set ``WSPR_FEED_HASHTABLE=1`` to re-enable the legacy
        # behaviour for A/B testing.
        ht_path = self.work_dir / "hashtable.txt"
        # Decode-health accounting: how long this cycle's decode took, and
        # whether wsprd was killed before it finished (see decode_health.py).
        _t0 = time.monotonic()
        _killed: List[bool] = []
        import os as _os
        if _os.environ.get("WSPR_FEED_HASHTABLE", "0") == "1":
            self.callsign_db.write_wsprd_hashtable(ht_path)

        # Run standard pass
        standard_spots = self._run_wsprd(
            wav_path, self.wsprd_path, spreading=False, killed=_killed,
            carrier_dir=carrier_p1,
        )

        # Run spreading pass — skipped when no spreading binary was
        # found on this host (wsprd_spread_path=None).  Returns the
        # standard spots unchanged from the merge step in that case.
        if self.wsprd_spread_path:
            spreading_spots = self._run_wsprd(
                wav_path, self.wsprd_spread_path, spreading=True, killed=_killed,
                carrier_dir=carrier_p2,
            )
        else:
            spreading_spots = []

        # Merge
        merged = self._merge_wspr_passes(standard_spots, spreading_spots)

        # Set pkt_mode
        for s in merged:
            s.pkt_mode = DECODE_MODE_PKT_MODES[DecodeMode.W2]

        # Ingest new callsigns
        calls_grids = [
            (s.call, s.grid) for s in merged
            if not s.call.startswith("<")
        ]
        new = self.callsign_db.ingest_spots(calls_grids, band=self.band_name)
        if new > 0:
            logger.info(f"{self.band_name}: {new} new callsigns from wsprd")

        # Also ingest from updated hashtable
        self.callsign_db.ingest_wsprd_hashtable(ht_path)

        # Truncate ALL_WSPR.TXT if too large
        self._truncate_all_wspr()

        self._record_health("W2", 120, cycle_start, _t0, killed=bool(_killed))
        return merged

    def decode_fst4w(self, wav_path: Path, period: int,
                     mode: DecodeMode,
                     cycle_start: Optional[float] = None) -> List[RawSpot]:
        """Decode an FST4W WAV (see ``_decode_fst4w``) and, when the carrier
        archive is on, file each decode's carrier afterwards."""
        self._check_input(wav_path, self.jt9_path)
        carrier_dir = self._carrier_scratch(self.jt9_path, jt9_has_carrier_dir,
                                            mode.value)
        try:
            return self._decode_fst4w(wav_path, period, mode, cycle_start,
                                      carrier_dir)
        finally:
            self._file_carriers([carrier_dir], mode.value, wav_path,
                                cycle_start)

    def _decode_fst4w(self, wav_path: Path, period: int, mode: DecodeMode,
                      cycle_start: Optional[float],
                      carrier_dir: Optional[Path] = None) -> List[RawSpot]:
        """
        Run jt9 -Y --fst4w on a period-length WAV file.

        1. Pre-populate fst4w_calls.txt from CallsignDB
        2. Run jt9 with -Y flag
        3. Parse new spots from fst4_decodes.dat (diff against previous)
        4. Resolve <NNNNNNN> numeric hashes via CallsignDB
        5. Ingest new callsigns

        Returns spots with appropriate pkt_mode.
        """
        # Same gate as for wsprd's hashtable.txt — let jt9 manage its
        # own ``fst4w_calls.txt`` across invocations unless the
        # operator opts back into CallsignDB-driven seeding.
        calls_path = self.work_dir / "fst4w_calls.txt"
        # Decode-health accounting (see decode_health.py)
        _t0 = time.monotonic()
        import os as _os
        if _os.environ.get("WSPR_FEED_HASHTABLE", "0") == "1":
            self.callsign_db.write_jt9_calls(calls_path)

        # Record current line count for diffing
        decodes_path = self.work_dir / "fst4_decodes.dat"
        prev_lines = self._count_lines(decodes_path)

        # Touch sentinel files that jt9 expects
        (self.work_dir / "plotspec").touch()
        (self.work_dir / "decdata").touch()

        # 110 s is the right wall-clock kill for a 2-minute slot.  It is the
        # WRONG one for F5/F15/F30: those have their own 5, 15 and 30 minute
        # cycles, they all come due together at :00 and :30, and killing jt9 at
        # 110 s throws away a decode that had minutes of headroom — silently,
        # since a killed decoder reports no spots.  So scale the kill with the
        # slot length, still bounded, so a wedged jt9 cannot hold a pool worker
        # indefinitely.  The 2-minute work queued behind it is not lost: the
        # pool runs short periods first and drains it afterwards.
        kill_after = (WSPRD_TIMEOUT_S if period <= 120
                      else _env_int("WSPR_LONG_DECODE_TIMEOUT_SEC", LONG_DECODE_TIMEOUT_S))
        # Run jt9
        # --fine-freq: fsig to 0.001 Hz (needs the plotspec touched above).
        fine = ["--fine-freq"] if jt9_has_fine_freq(self.jt9_path) else []
        # --carrier-dir: each decode's carrier (carrier_archive.py); only
        # when the archive is on and this jt9 has it (see decode_fst4w).
        if carrier_dir is not None:
            fine += ["--carrier-dir", str(carrier_dir)]
        cmd = [
            "nice", "-n", "19",
            "timeout", str(kill_after),
            self.jt9_path,
            "-Y",
            "-a", str(self.work_dir),
            "--fst4w", *fine,
            "-p", str(period),
            "-f", "1500",
            "-F", "100",
            str(wav_path),
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=kill_after + 10,
                cwd=str(self.work_dir),
            )
            if result.returncode != 0 and result.returncode != 1:
                logger.warning(
                    f"{self.band_name}: jt9 exited {result.returncode}: "
                    f"{result.stderr[:200]}"
                )
            # 124 is the 'timeout 110' above killing jt9 mid-decode: this
            # period produced no spots, which is a lost cycle, not a quiet one.
            if result.returncode == 124:
                self._record_health(mode.value, period, cycle_start, _t0, killed=True)
                return []
        except subprocess.TimeoutExpired:
            logger.warning(f"{self.band_name}: jt9 timed out")
            self._record_health(mode.value, period, cycle_start, _t0, killed=True)
            return []
        except FileNotFoundError:
            logger.error(f"{self.band_name}: jt9 not found at {self.jt9_path}")
            return []

        # Parse new spots from fst4_decodes.dat
        spots = self._parse_new_fst4_decodes(decodes_path, prev_lines)

        # Set pkt_mode
        pkt_mode = DECODE_MODE_PKT_MODES[mode]
        for s in spots:
            s.pkt_mode = pkt_mode

        # Resolve -Y numeric hashes
        resolved_count = 0
        for s in spots:
            if s.hash22 is not None:
                call = self.callsign_db.resolve_hash22(s.hash22)
                if call:
                    s.call = call
                    s.hash22 = None
                    resolved_count += 1

        if resolved_count > 0:
            logger.info(
                f"{self.band_name}: Resolved {resolved_count} type-3 hashes "
                f"via CallsignDB"
            )

        # Ingest new callsigns
        calls_grids = [
            (s.call, s.grid) for s in spots
            if not s.call.startswith("<")
        ]
        new = self.callsign_db.ingest_spots(calls_grids, band=self.band_name)
        if new > 0:
            logger.info(f"{self.band_name}: {new} new callsigns from jt9")

        self._record_health(mode.value, period, cycle_start, _t0, killed=False)
        return spots

    # ------------------------------------------------------------------
    # Carrier archive (carrier_archive.py)
    # ------------------------------------------------------------------

    def _carrier_scratch(self, binary: Optional[str], has_option,
                         label: str) -> Optional[Path]:
        """A scratch dir for `binary`'s carriers, or None: archive off (no
        probe is even run), the binary lacks the option, or no dir could be
        made.  None leaves the command line exactly as it was."""
        if self.carrier_archive is None or not binary:
            return None
        try:
            if not has_option(binary):
                return None
            return self.carrier_archive.scratch_dir(self.work_dir, label)
        except Exception:                                    # noqa: BLE001
            logger.warning("%s: carrier archive: probe of %s failed",
                           self.band_name, binary, exc_info=True)
            return None

    def _file_carriers(self, dirs: List[Optional[Path]], mode: str,
                       wav_path: Path, cycle_start: Optional[float]) -> None:
        """File what the decoders wrote into `dirs` (most preferred first).
        Never raises: a filing problem must not cost the decode its spots."""
        if self.carrier_archive is None or not any(dirs):
            return
        try:
            self.carrier_archive.file_cycle(
                dirs, band=self.band_name, mode=mode,
                when=self._cycle_time(wav_path, cycle_start),
                dial_hz=self.frequency_hz,
            )
        except Exception:                                    # noqa: BLE001
            logger.warning("%s %s: carrier archive: filing failed",
                           self.band_name, mode, exc_info=True)
        finally:
            CarrierArchive.discard(dirs)

    @staticmethod
    def _cycle_time(wav_path: Path, cycle_start: Optional[float]) -> datetime:
        """The cycle's UTC start: `cycle_start`, else the decoder WAV's
        ``YYMMDD_HHMM`` name, else now."""
        if cycle_start is not None:
            return datetime.fromtimestamp(cycle_start, tz=timezone.utc)
        m = re.match(r"^(\d{6})_(\d{4})", Path(wav_path).name)
        if m:
            try:
                return datetime.strptime("".join(m.groups()), "%y%m%d%H%M"
                                         ).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    def _record_health(self, mode: str, period_s: int,
                       cycle_start: Optional[float], t0: float,
                       *, killed: bool) -> None:
        """File one decode outcome with the decode-health ledger.

        ``cycle_start`` is the wall-clock start of the audio slice, so the
        audio was complete at ``cycle_start + period_s`` and everything after
        that is this decode running late.  Callers that don't know the cycle
        (tests, decode_cycle) pass None and only the elapsed/killed half is
        recorded.  Never raises: health accounting must not break a decode.
        """
        try:
            elapsed = time.monotonic() - t0
            HEALTH.record(
                band=self.band_name, mode=mode, period_s=period_s,
                cycle_start=(cycle_start if cycle_start is not None
                             else time.time() - elapsed - period_s),
                decode_start=time.time() - elapsed,
                elapsed_s=elapsed, killed=killed,
            )
        except Exception:                                    # noqa: BLE001
            logger.debug("decode-health: could not record outcome", exc_info=True)

    def decode_cycle(self, wav_path: Path,
                     modes: List[DecodeMode]) -> List[RawSpot]:
        """
        Full decode cycle for one period boundary.

        Runs wsprd first (discovers callsigns), then jt9 -Y (can resolve
        type-3 using callsigns just discovered). Returns combined spots.
        """
        all_spots: List[RawSpot] = []

        # WSPR-2 first (enriches CallsignDB before FST4W)
        if DecodeMode.W2 in modes:
            all_spots.extend(self.decode_wspr(wav_path))

        # FST4W modes
        from .decode_mode import DECODE_MODE_PERIODS
        for mode in modes:
            if mode == DecodeMode.W2:
                continue
            period = DECODE_MODE_PERIODS[mode]
            all_spots.extend(self.decode_fst4w(wav_path, period, mode))

        return all_spots

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_wsprd(self, wav_path: Path, wsprd_bin: str,
                   spreading: bool = False,
                   killed: Optional[List[bool]] = None,
                   carrier_dir: Optional[Path] = None) -> List[RawSpot]:
        """Run a single wsprd pass and parse ALL_WSPR.TXT for new spots."""
        all_wspr_path = self.work_dir / "ALL_WSPR.TXT"
        prev_lines = self._count_lines(all_wspr_path)

        cmd = [
            wsprd_bin,
            "-c",           # write .c2 file (for noise measurement)
            "-C", "500",    # hash table size
            "-o", "4",      # subtraction passes
            "-d",           # deep search
        ]
        # Note: the spreading variant (wsprd.spread / wsprd.spread_nodrift) does
        # NOT need a -n flag — the no-drift behavior is built into the binary.
        # v3 conditionally passes -n on some platforms but the v27 binary doesn't
        # accept it.
        if wsprd_has_fine_freq(wsprd_bin):
            cmd.append("-F")       # ALL_WSPR.TXT's frequency to 0.001 Hz
        if carrier_dir is not None:  # each decode's carrier (carrier_archive.py)
            cmd.extend(["-I", str(carrier_dir)])
        cmd.extend(["-f", f"{self.freq_mhz:.6f}", str(wav_path)])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                cwd=str(self.work_dir),
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"{self.band_name}: wsprd timed out")
            if killed is not None:
                killed.append(True)
            return []
        except FileNotFoundError:
            logger.error(f"{self.band_name}: wsprd not found at {wsprd_bin}")
            return []

        # Parse new lines from ALL_WSPR.TXT
        return self._parse_new_all_wspr(all_wspr_path, prev_lines, spreading)

    def _parse_new_all_wspr(self, path: Path, prev_lines: int,
                            spreading: bool) -> List[RawSpot]:
        """Parse new lines appended to ALL_WSPR.TXT since prev_lines."""
        if not path.exists():
            return []

        spots = []
        with open(path, 'r') as f:
            for i, line in enumerate(f):
                if i < prev_lines:
                    continue
                spot = self._parse_all_wspr_line(line.strip(), spreading)
                if spot:
                    spots.append(spot)
        return spots

    def _parse_all_wspr_line(self, line: str,
                             spreading: bool = False) -> Optional[RawSpot]:
        """
        Parse one line from ALL_WSPR.TXT.

        Format (17-19 fields):
        date time snr dt freq message drift sync ipass blocksize jitter
        decodetype nhardmin cycles metric [spreading]

        The 'message' is typically "CALL GRID POWER" (3 tokens) but can be
        other formats for type-2/type-3.
        """
        parts = line.split()
        if len(parts) < 10:
            return None

        try:
            date = parts[0]     # YYMMDD
            time = parts[1]     # HHMM
            snr = int(parts[2])
            dt = float(parts[3])
            freq = float(parts[4])

            # Message parsing: find call, grid, power
            # The message starts at parts[5] and is variable length
            # We need to find drift (int), sync (float) after the message
            # Strategy: work backward from known numeric fields
            # The last fields are: drift sync ipass blocksize jitter decodetype nhardmin cycles metric
            # That's 9 fields after the message

            # Find where the message ends by counting backward
            # Minimum: 9 trailing fields after message
            # Message is at least 1 token (could be "CALL GRID POWER" = 3 tokens)
            remaining = parts[5:]

            # Try to parse as "CALL GRID POWER trailing..."
            call = remaining[0]
            grid = ""
            power = 0
            msg_tokens = 1

            if len(remaining) > 2:
                # Check if remaining[1] looks like a grid (4-6 alphanumeric)
                if (len(remaining[1]) in (4, 6) and
                        remaining[1][:2].isalpha() and remaining[1][2:4].isdigit()):
                    grid = remaining[1]
                    try:
                        power = int(remaining[2])
                        msg_tokens = 3
                    except (ValueError, IndexError):
                        msg_tokens = 2
                else:
                    try:
                        power = int(remaining[1])
                        msg_tokens = 2
                    except ValueError:
                        msg_tokens = 1

            trailing = remaining[msg_tokens:]

            # Parse trailing fields (best effort)
            drift = int(trailing[0]) if len(trailing) > 0 else 0
            sync_quality = float(trailing[1]) if len(trailing) > 1 else 0.0
            ipass = int(trailing[2]) if len(trailing) > 2 else 0
            blocksize = int(trailing[3]) if len(trailing) > 3 else 0
            jitter = int(trailing[4]) if len(trailing) > 4 else 0
            decodetype = int(trailing[5]) if len(trailing) > 5 else 0
            nhardmin = int(trailing[6]) if len(trailing) > 6 else 0
            cycles = int(trailing[7]) if len(trailing) > 7 else 0
            metric = float(trailing[8]) if len(trailing) > 8 else 0.0

            spread_val = None
            if spreading and len(trailing) > 9:
                try:
                    spread_val = float(trailing[9])
                except ValueError:
                    pass

            return RawSpot(
                date=date, time=time, snr=snr, dt=dt, freq=freq,
                call=call, grid=grid, power=power, drift=drift,
                sync_quality=sync_quality, ipass=ipass, blocksize=blocksize,
                jitter=jitter, decodetype=decodetype, nhardmin=nhardmin,
                cycles=cycles, metric=metric, spreading=spread_val,
            )
        except (ValueError, IndexError) as e:
            logger.debug(f"Failed to parse ALL_WSPR.TXT line: {e}: {line}")
            return None

    def _parse_new_fst4_decodes(self, path: Path,
                                prev_lines: int) -> List[RawSpot]:
        """Parse new lines from fst4_decodes.dat since prev_lines."""
        if not path.exists():
            return []

        spots = []
        with open(path, 'r') as f:
            for i, line in enumerate(f):
                if i < prev_lines:
                    continue
                spot = self._parse_fst4_line(line.strip())
                if spot:
                    spots.append(spot)
        return spots

    def _parse_fst4_line(self, line: str) -> Optional[RawSpot]:
        """
        Parse one line from fst4_decodes.dat.

        WSJT-X ``lib/fst4_decode.f90`` (format 3021) writes, per decode::

            nutc icand itry nsyncoh iaptype ijitter npct ntype Keff nsync_qual
            nharderrors dmin nhp hd sync xsnr xdt fsig w50 <message…>

        ``nutc`` is ``i6.6`` (zero-padded HHMMSS, e.g. ``000526``) and ``fsig``
        is the AUDIO offset in Hz (``f7.1``, e.g. ``1535.0``) — not an absolute
        frequency.  The message is ``CALL GRID PWR``, ``<hash> GRID6 PWR`` (-Y)
        or ``CALL PWR``.

        Until 2026-10-28 this parser assumed no ``nutc`` column and an absolute
        ``fsig``: every index was off by one, ``int("CM88")`` raised, and every
        FST4W decode jt9 produced was dropped — no sigmond site had ever
        uploaded an FST4W spot (KJ6MKI migration, 40 m: 254/254 lines lost).
        The historical 18-numeric-column form (no ``nutc``) is still accepted.
        """
        parts = line.split()
        if len(parts) < 20:
            return None
        off = 1 if _NUTC_RE.fullmatch(parts[0]) else 0
        if len(parts) < 20 + off:
            return None

        try:
            snr = int(round(float(parts[14 + off])))    # xsnr
            dt = float(parts[15 + off])                  # xdt
            # fsig: audio offset (Hz); f7.1, or f9.3 with --fine-freq
            fsig_hz = float(parts[16 + off])
            # jt9's fsig is relative to the WAV's 0 Hz, i.e. to this band's
            # dial.  Keep accepting an absolute value (old fixtures) by size.
            freq_hz = fsig_hz if fsig_hz >= 100_000.0 else self.frequency_hz + fsig_hz
            freq_mhz = freq_hz / 1e6
            msg = parts[18 + off:]                       # call [grid] [power]
            call = msg[0]
            grid = msg[1] if len(msg) > 1 else ""
            power = int(msg[2]) if len(msg) > 2 else 0
            if len(msg) == 2 and grid.lstrip("+-").isdigit():
                power, grid = int(grid), ""              # "CALL PWR" (no grid)

            # Check for -Y numeric hash
            hash22 = None
            m = _HASH22_RE.match(call)
            if m:
                hash22 = int(m.group(1))

            # Derive date/time from context (use placeholder, will be set by caller)
            sync_quality = float(parts[13 + off])  # sync
            nhardmin = int(parts[9 + off])         # nharderrors
            # w50: the spread, the width holding half the power, in Hz
            # (the column after fsig).  wsprdaemon's decoding.sh carries
            # it as the metric ("19 w50 ... metric (repurposed)"), and the
            # uploader sends metric*1000, i.e. mHz.  Until 2026-11-23 this
            # read dmin (column 10+off), which reached wsprdaemon.org as
            # 3000-94600 "spreads" (G3ZIL).
            metric = float(parts[17 + off])        # w50

            return RawSpot(
                date="", time="",  # filled by caller from WAV filename
                snr=snr, dt=dt, freq=freq_mhz,
                call=call, grid=grid, power=power,
                sync_quality=sync_quality,
                nhardmin=nhardmin, metric=metric,
                hash22=hash22,
            )
        except (ValueError, IndexError) as e:
            logger.debug(f"Failed to parse fst4_decodes.dat line: {e}: {line}")
            return None

    def _merge_wspr_passes(self, standard: List[RawSpot],
                           spreading: List[RawSpot]) -> List[RawSpot]:
        """
        Merge standard and spreading wsprd passes.

        For each TX callsign:
        - If spreading pass has the call, prefer it (has spreading data)
        - If both have it, prefer spreading; if same, pick best SNR
        - If only standard has it, use standard
        """
        # Index spreading spots by callsign
        spread_by_call: dict[str, RawSpot] = {}
        for s in spreading:
            if s.call not in spread_by_call or s.snr > spread_by_call[s.call].snr:
                spread_by_call[s.call] = s

        # Build merged list
        merged: dict[str, RawSpot] = {}
        for s in standard:
            if s.call in spread_by_call:
                # Prefer spreading version (has spreading metric)
                sp = spread_by_call[s.call]
                # Copy spreading value to metric field
                if sp.spreading is not None:
                    sp.metric = sp.spreading
                merged[s.call] = sp
            else:
                merged[s.call] = s

        # Add any spreading-only spots
        for call, s in spread_by_call.items():
            if call not in merged:
                if s.spreading is not None:
                    s.metric = s.spreading
                merged[call] = s

        return list(merged.values())

    def _truncate_all_wspr(self) -> None:
        """Truncate ALL_WSPR.TXT if it exceeds MAX_ALL_WSPR_SIZE."""
        path = self.work_dir / "ALL_WSPR.TXT"
        if not path.exists():
            return
        try:
            size = path.stat().st_size
            if size > MAX_ALL_WSPR_SIZE:
                # Keep the last MAX_ALL_WSPR_SIZE bytes
                with open(path, 'rb') as f:
                    f.seek(size - MAX_ALL_WSPR_SIZE)
                    # Skip to next complete line
                    f.readline()
                    data = f.read()
                with open(path, 'wb') as f:
                    f.write(data)
                logger.debug(
                    f"{self.band_name}: Truncated ALL_WSPR.TXT "
                    f"from {size} to {len(data)} bytes"
                )
        except Exception as e:
            logger.warning(f"Failed to truncate ALL_WSPR.TXT: {e}")

    @staticmethod
    def _count_lines(path: Path) -> int:
        """Count lines in a file (0 if file doesn't exist)."""
        if not path.exists():
            return 0
        with open(path, 'r') as f:
            return sum(1 for _ in f)
