"""Carrier archive: each WSPR / FST4W spot's carrier, its modulation removed.

Once wsprd or jt9 has decoded a spot it knows the tones, so the ubersdr
builds (5eeeb53 and later, madpsy PR #6) can multiply the samples by the
conjugate of the decoded signal and write what is left, the bare carrier: a
2-channel 32-bit float IQ .wav at 15 Hz (about 13 KB for a WSPR-2 spot), with
the decode's particulars as JSON in its LIST/INFO/ICMT chunk.  The options
are ``wsprd -I DIR`` and ``jt9 --fst4w --carrier-dir DIR``; in DIR each
decode becomes ``CALL_AUDIOHZ.wav`` (carrier_iq.cpp file_name: the call with
``<>`` taken off and every non-alphanumeric as ``_``, the audio frequency
``%.3f``).

After each decode this module files those files, as wsprdaemon's
carrier-archive.sh does, at::

    ROOT/YYYYMMDD/RECEIVER/BAND/HHMM_MODE_CALL_RFHZ.wav

RFHZ is the dial plus the audio frequency, to 0.001 Hz; MODE is W2, F2, F5,
F15 or F30.  Where two wsprd passes both wrote a carrier for one call, the
-n (no-drift, spreading) pass's wins, the same preference
``DecoderRunner._merge_wspr_passes`` gives its spot.

Only the calls listed in ``calls`` are filed (``[carrier_archive] calls``,
``["WW0WWV"]`` by default in the config; matched on the base call, so
``WW0WWV/P`` counts); every other carrier is discarded with the scratch
directory.  ``calls`` of None, ``[]`` or ``["*"]`` files every call.

``purge`` deletes day directories past ``max_age_days`` and then, while the
file system is fuller than ``max_disk_pct``, the oldest days (never the
newest).  It runs from the recorder's cleanup timer, never in a decode.

Nothing here may break a decode or an upload: every error is logged and
swallowed.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

# CALL_AUDIOHZ.wav as the decoder names it (the call may itself hold '_').
_CARRIER_NAME_RE = re.compile(r"^(?P<call>.+)_(?P<hz>\d+(?:\.\d+)?)\.wav$")
# A root is only purged if it holds this file, which the recorder writes when
# it creates the root (or first uses an empty / archive-shaped one): purge
# can then never empty a directory that was not made for the archive.
MARKER = ".wspr-carrier-archive"
# Filing stops at max_disk_pct + HARD_LIMIT_MARGIN, never above HARD_LIMIT_CAP.
HARD_LIMIT_MARGIN = 5.0
HARD_LIMIT_CAP = 98.0
# Roots already warned about (no marker): one warning per root per process.
_WARNED_NO_MARKER: set = set()
_DAY_RE = re.compile(r"^\d{8}$")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._=-]+")

PathLike = Union[str, Path]
_CALL_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


def base_call(call: str) -> str:
    """The base callsign of ``call``, upper-cased: of the parts split at any
    non-alphanumeric (``/`` in a call, ``_`` in a decoder's file name), the
    longest that holds a digit, else the longest.  ``WW0WWV/P`` and
    ``WW0WWV_P`` are ``WW0WWV``; ``PJ4/K1ABC`` is ``K1ABC``."""
    parts = [p for p in _CALL_SPLIT_RE.split((call or "").upper()) if p]
    if not parts:
        return ""
    with_digit = [p for p in parts if any(c.isdigit() for c in p)
                  and any(c.isalpha() for c in p)]
    return max(with_digit or parts, key=len)


def call_filter(calls: Optional[Sequence[str]]) -> Optional[frozenset]:
    """The set of base calls to file, or None for every call (``calls`` is
    None, empty, or holds ``"*"``)."""
    if not calls or any(str(c).strip() == "*" for c in calls):
        return None
    return frozenset(b for b in (base_call(str(c)) for c in calls) if b)


def safe_name(name: str, fallback: str = "receiver") -> str:
    """``name`` as one safe path component: anything but letters, digits,
    ``. _ = -`` becomes ``-`` (so ``AC0G/S`` is ``AC0G-S``), and leading or
    trailing dots and dashes go, so it can never be ``..``."""
    s = _UNSAFE_RE.sub("-", name or "").strip(".-")
    return s or fallback


def disk_used_pct(path: PathLike) -> Optional[float]:
    """Percent of the file system holding ``path`` in use, as df reports it
    (used / (used + available to non-root)); None if it cannot be read."""
    try:
        u = shutil.disk_usage(str(path))
    except OSError:
        return None
    denom = u.used + u.free
    if denom <= 0:
        return None
    return 100.0 * u.used / denom


def _day_dirs(root: Path) -> List[Path]:
    """root's YYYYMMDD directories, oldest first.  Symlinks are skipped:
    purge must never delete anything outside the root."""
    out = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for e in entries:
        try:
            if _DAY_RE.match(e.name) and e.is_dir(follow_symlinks=False):
                out.append(root / e.name)
        except OSError:
            continue
    return sorted(out, key=lambda p: p.name)


def _rmtree(path: Path, why: str) -> bool:
    try:
        shutil.rmtree(path)
    except OSError as exc:
        logger.warning("carrier archive: could not delete %s (%s): %s",
                       path, why, exc)
        return False
    logger.info("carrier archive: deleted %s: %s", path, why)
    return True


def purge(root: PathLike, *, max_age_days: int, max_disk_pct: float,
          now: Optional[datetime] = None,
          disk_used_pct: Callable[[PathLike], Optional[float]] = disk_used_pct,
          ) -> List[str]:
    """Delete day directories older than ``max_age_days`` (whole UTC days,
    today included) or dated after today (a clock jump), then the oldest
    while the file system holding ``root`` is fuller than ``max_disk_pct``.
    The disk pass never deletes today's directory nor the newest one; if
    only that is left, filing's hard limit takes over.  Nothing is done
    unless ``root`` holds the MARKER file.  Returns the paths removed.
    Never raises."""
    removed: List[str] = []
    try:
        root = Path(root)
        if not root.is_dir():
            return removed
        if not (root / MARKER).is_file():
            if str(root) not in _WARNED_NO_MARKER:
                _WARNED_NO_MARKER.add(str(root))
                logger.warning("carrier archive: not purging %s: it has no %s "
                               "file, so it is not known to be the archive",
                               root, MARKER)
            return removed
        now = now or datetime.now(timezone.utc)
        today_d = now.astimezone(timezone.utc).date()
        today = today_d.strftime("%Y%m%d")
        oldest_kept = (today_d - timedelta(days=max_age_days - 1)).strftime("%Y%m%d")
        for d in _day_dirs(root):
            if d.name < oldest_kept:
                why = f"older than max_age_days={max_age_days}"
            elif d.name > today:
                why = f"dated after today ({today}): a clock jump?"
            else:
                continue
            if _rmtree(d, why):
                removed.append(str(d))
        while True:
            pct = disk_used_pct(root)
            if pct is None or pct <= max_disk_pct:
                break
            days = _day_dirs(root)
            candidates = [d for d in days[:-1] if d.name < today]
            if not candidates:
                break
            if not _rmtree(candidates[0], f"file system {pct:.0f}% used, over "
                                          f"max_disk_pct={max_disk_pct}"):
                break
            removed.append(str(candidates[0]))
    except Exception:                                      # noqa: BLE001
        logger.warning("carrier archive: purge failed", exc_info=True)
    return removed


class CarrierArchive:
    """One receiver's view of the archive: where its decoders' carriers go."""

    SCRATCH_DIR = "carrier.d"

    def __init__(self, root: PathLike, receiver: str, *,
                 max_age_days: int = 3, max_disk_pct: float = 90,
                 calls: Optional[Sequence[str]] = None,
                 disk_used_pct: Callable[[PathLike], Optional[float]] = disk_used_pct):
        """``calls``: the calls whose carriers are filed (base call, any
        case); None, ``[]`` or ``["*"]`` files them all.  The config's
        default is ``["WW0WWV"]`` (CarrierArchiveConfig)."""
        self.root = Path(root)
        self.calls = call_filter(calls)
        self.receiver = safe_name(receiver)
        self.max_age_days = max_age_days
        self.max_disk_pct = max_disk_pct
        self._disk_used_pct = disk_used_pct
        self._root_ok = False         # MARKER seen or written
        self._root_warned = False
        self._full = False            # over the hard limit last time we looked

    @property
    def hard_limit_pct(self) -> float:
        """Disk use at which filing stops: max_disk_pct + 5, at most 98."""
        return min(float(self.max_disk_pct) + HARD_LIMIT_MARGIN, HARD_LIMIT_CAP)

    def ensure_root(self) -> bool:
        """Create the root if needed and stamp it with MARKER.  An existing
        root is stamped only if it is empty or holds nothing but day
        directories; otherwise it is left alone (filing still works, purge
        refuses) with one warning.  True iff the root carries MARKER."""
        if self._root_ok:
            return True
        try:
            marker = self.root / MARKER
            if not marker.is_file():
                if self.root.exists():
                    foreign = [n for n in os.listdir(self.root)
                               if not (_DAY_RE.match(n) and (self.root / n).is_dir()
                                       and not (self.root / n).is_symlink())]
                    if foreign:
                        if not self._root_warned:
                            self._root_warned = True
                            logger.warning(
                                "carrier archive: %s already holds other files "
                                "(%s ...); not marking it, so it will never be "
                                "purged.  Point [carrier_archive] root at a "
                                "directory of its own.", self.root, foreign[0])
                        return False
                self.root.mkdir(parents=True, exist_ok=True)
                marker.touch()
            self._root_ok = True
        except OSError as exc:
            if not self._root_warned:
                self._root_warned = True
                logger.warning("carrier archive: cannot set up %s: %s",
                               self.root, exc)
            return False
        return True

    def _existing(self) -> Path:
        p = self.root
        while not p.exists() and p != p.parent:
            p = p.parent
        return p

    def _disk_ok(self) -> bool:
        """False, logged once until it clears, while the file system holding
        the root is at or over the hard limit."""
        try:
            pct = self._disk_used_pct(self._existing())
        except Exception:                                  # noqa: BLE001
            pct = None
        if pct is not None and pct >= self.hard_limit_pct:
            if not self._full:
                self._full = True
                logger.warning(
                    "carrier archive: file system holding %s is %.0f%% full "
                    "(hard limit %.0f%%): not filing carriers until it is "
                    "below", self.root, pct, self.hard_limit_pct)
            return False
        if self._full:
            self._full = False
            logger.info("carrier archive: %s below %.0f%% again; filing "
                        "resumed", self.root, self.hard_limit_pct)
        return True

    @classmethod
    def sweep_scratch(cls, work_dir: PathLike, older_than_s: float = 3600) -> int:
        """Remove scratch dirs under ``work_dir/carrier.d`` older than
        ``older_than_s`` (left by a recorder that died mid-decode; the WAV
        cleanup never looks inside carrier.d).  Returns how many.  Never
        raises."""
        import time
        n = 0
        base = Path(work_dir) / cls.SCRATCH_DIR
        cutoff = time.time() - older_than_s
        try:
            entries = list(os.scandir(base))
        except OSError:
            return 0
        for e in entries:
            try:
                if (e.is_dir(follow_symlinks=False)
                        and e.stat(follow_symlinks=False).st_mtime < cutoff):
                    shutil.rmtree(e.path, ignore_errors=True)
                    n += 1
            except OSError:
                continue
        return n

    @classmethod
    def from_config(cls, cfg, receiver: str) -> Optional["CarrierArchive"]:
        """A CarrierArchive for ``cfg`` (a CarrierArchiveConfig), or None
        when the archive is disabled."""
        if getattr(cfg, "enabled", False) is not True:
            return None
        return cls(cfg.root, receiver, max_age_days=cfg.max_age_days,
                   max_disk_pct=cfg.max_disk_pct,
                   calls=getattr(cfg, "calls", None))

    def wants(self, call: str) -> bool:
        """True iff a carrier of ``call`` (as the decoder names the file:
        ``/`` already ``_``) is to be filed."""
        return self.calls is None or base_call(call) in self.calls

    def scratch_dir(self, work_dir: PathLike, label: str) -> Optional[Path]:
        """A fresh, empty directory for one decoder run to write carriers
        into: ``work_dir/carrier.d/.<label>.XXXX``.  Unique per call, since
        a band's W2 and F5 decodes can run at once in one work_dir; dotted,
        so WavWriter's per-band cap never takes it for a band directory.
        None if it cannot be made (the decode then runs without the
        option)."""
        try:
            base = Path(work_dir) / self.SCRATCH_DIR
            base.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix=f".{safe_name(label)}.",
                                         dir=str(base))).resolve()
        except OSError as exc:
            logger.warning("carrier archive: no scratch dir in %s: %s",
                           work_dir, exc)
            return None

    def file_cycle(self, src_dirs: Sequence[Optional[PathLike]], *,
                   band: str, when: datetime, mode: str, dial_hz: float) -> int:
        """Move every carrier in ``src_dirs`` into the archive and remove
        the directories.  ``src_dirs`` are in order of preference: the
        first to hold a call's carrier wins (list the -n pass first).
        Returns the number filed.  Skips the cycle while the file system is
        at the hard limit.  Failures are logged once per cycle.  Never
        raises."""
        count = 0
        failed = 0
        first_error = None
        try:
            if not any(s is not None for s in src_dirs) or not self._disk_ok():
                return 0
            self.ensure_root()
            when = when.astimezone(timezone.utc)
            dest = (self.root / when.strftime("%Y%m%d") / self.receiver
                    / safe_name(band, "band"))
            prefix = f"{when.strftime('%H%M')}_{safe_name(mode, 'mode')}_"
            filed = set()
            for src in src_dirs:
                if src is None:
                    continue
                src = Path(src)
                try:
                    names = sorted(os.listdir(src))
                except OSError:
                    continue
                for name in names:
                    m = _CARRIER_NAME_RE.match(name)
                    if not m:
                        continue
                    call = m.group("call")
                    if not self.wants(call):
                        continue                  # discarded with src
                    if call in filed:
                        continue                  # a preferred pass had it
                    filed.add(call)
                    rf_hz = float(dial_hz) + float(m.group("hz"))
                    target = dest / f"{prefix}{safe_name(call, 'call')}_{rf_hz:.3f}.wav"
                    existed = target.exists()
                    try:
                        dest.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src / name), str(target))
                        count += 1
                    except OSError as exc:
                        failed += 1
                        if first_error is None:
                            first_error = f"{target}: {exc}"
                        if not existed:      # a cross-fs copy cut short
                            try:
                                target.unlink(missing_ok=True)
                            except OSError:
                                pass
            if failed:
                logger.warning("carrier archive: %d of %d %s carriers of %s %s "
                               "%s not filed; first error: %s", failed,
                               failed + count, mode, self.receiver, band,
                               when.strftime("%Y%m%d %H%M"), first_error)
            if count:
                logger.debug("carrier archive: filed %d %s carriers of %s %s "
                             "%s in %s", count, mode, self.receiver, band,
                             when.strftime("%Y%m%d %H%M"), dest)
        except Exception:                                  # noqa: BLE001
            logger.warning("carrier archive: filing failed", exc_info=True)
        finally:
            self.discard(src_dirs)
        return count

    @staticmethod
    def discard(src_dirs: Sequence[Optional[PathLike]]) -> None:
        for src in src_dirs:
            if src is not None:
                shutil.rmtree(str(src), ignore_errors=True)

    def purge(self, now: Optional[datetime] = None) -> List[str]:
        return purge(self.root, max_age_days=self.max_age_days,
                     max_disk_pct=self.max_disk_pct, now=now,
                     disk_used_pct=self._disk_used_pct)
