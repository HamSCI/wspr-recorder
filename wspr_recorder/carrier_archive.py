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
_DAY_RE = re.compile(r"^\d{8}$")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._=-]+")

PathLike = Union[str, Path]


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
    today included), then the oldest while the file system holding ``root``
    is fuller than ``max_disk_pct``; the newest day is never deleted.
    Returns the paths removed.  Never raises."""
    removed: List[str] = []
    try:
        root = Path(root)
        if not root.is_dir():
            return removed
        now = now or datetime.now(timezone.utc)
        oldest_kept = (now.astimezone(timezone.utc).date()
                       - timedelta(days=max_age_days - 1)).strftime("%Y%m%d")
        for d in _day_dirs(root):
            if d.name < oldest_kept and _rmtree(
                    d, f"older than max_age_days={max_age_days}"):
                removed.append(str(d))
        while True:
            pct = disk_used_pct(root)
            if pct is None or pct <= max_disk_pct:
                break
            days = _day_dirs(root)
            if len(days) <= 1:
                break
            if not _rmtree(days[0], f"file system {pct:.0f}% used, over "
                                    f"max_disk_pct={max_disk_pct}"):
                break
            removed.append(str(days[0]))
    except Exception:                                      # noqa: BLE001
        logger.warning("carrier archive: purge failed", exc_info=True)
    return removed


class CarrierArchive:
    """One receiver's view of the archive: where its decoders' carriers go."""

    SCRATCH_DIR = "carrier.d"

    def __init__(self, root: PathLike, receiver: str, *,
                 max_age_days: int = 3, max_disk_pct: float = 90,
                 disk_used_pct: Callable[[PathLike], Optional[float]] = disk_used_pct):
        self.root = Path(root)
        self.receiver = safe_name(receiver)
        self.max_age_days = max_age_days
        self.max_disk_pct = max_disk_pct
        self._disk_used_pct = disk_used_pct

    @classmethod
    def from_config(cls, cfg, receiver: str) -> Optional["CarrierArchive"]:
        """A CarrierArchive for ``cfg`` (a CarrierArchiveConfig), or None
        when the archive is disabled."""
        if getattr(cfg, "enabled", False) is not True:
            return None
        return cls(cfg.root, receiver, max_age_days=cfg.max_age_days,
                   max_disk_pct=cfg.max_disk_pct)

    def scratch_dir(self, work_dir: PathLike, label: str) -> Optional[Path]:
        """A fresh, empty directory for one decoder run to write carriers
        into: ``work_dir/carrier.d/<label>.XXXX``.  Unique per call, since
        a band's W2 and F5 decodes can run at once in one work_dir.  None
        if it cannot be made (the decode then runs without the option)."""
        try:
            base = Path(work_dir) / self.SCRATCH_DIR
            base.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix=f"{safe_name(label)}.",
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
        Returns the number filed.  Never raises."""
        count = 0
        try:
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
                    if call in filed:
                        continue                  # a preferred pass had it
                    filed.add(call)
                    rf_hz = float(dial_hz) + float(m.group("hz"))
                    target = dest / f"{prefix}{safe_name(call, 'call')}_{rf_hz:.3f}.wav"
                    try:
                        dest.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src / name), str(target))
                        count += 1
                    except OSError as exc:
                        logger.warning("carrier archive: could not file %s "
                                       "as %s: %s", src / name, target, exc)
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
