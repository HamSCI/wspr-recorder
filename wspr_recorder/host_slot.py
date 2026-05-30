"""Host-wide cross-process semaphore for memory-bounding the F-mode decode wave.

Every `wspr-recorder@<reporter-id>.service` instance on a host coordinates
via these locks so the F15 (900 s) and F30 (1800 s) decoders never run
simultaneously across the whole fleet — without this, a multi-receiver
deployment (e.g. 4 triplets = 12 instances × 2 F30 bands) would hold
~2 GB of in-memory float32 slices at the top-of-hour boundary even with
per-instance worker caps.

The slot count is a constant per period; each lock file lives in
``/var/lib/wspr-recorder/`` so every recorder user (wsprrec) can
``fcntl.lockf`` byte-ranges within it.  ``lockf`` is atomic per range
and is released automatically on process death — operators don't need
to clean up stale slots after a crash.

W2 / F2 (120 s) and F5 (300 s) are left unbounded.  Their slices are
small (5.76 MB and 14.4 MB respectively) and contention is high
(every minute / every 5 minutes); serializing them would add latency
without much memory benefit.

See ``__main__.py:_on_period_complete`` for the use site.  See
``band_recorder.py`` for the matching deferred-slice path that prevents
the in-memory slice from being allocated until the slot is acquired.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import time
from pathlib import Path
from typing import Generator, Optional, Tuple


logger = logging.getLogger(__name__)


# Host-wide slot counts.  Tuned so the worst-case top-of-hour wave fits
# inside the band-recorder ring headroom (RING_HEADROOM_SECONDS in
# band_recorder.py):
#
#   12 instances × 2 F30 bands / 2 slots × 30 s/decode ≈ 6 min
#   12 instances × 2 F15 bands / 4 slots × 15 s/decode ≈ 1.5 min
#
# Override with WSPR_F30_SLOTS / WSPR_F15_SLOTS for sites with tighter
# memory budgets or smaller fleets.
F30_SLOTS = int(os.environ.get("WSPR_F30_SLOTS") or 2)
F15_SLOTS = int(os.environ.get("WSPR_F15_SLOTS") or 4)


# Lock dir.  Must exist and be writable by every recorder service user.
# /var/lib/wspr-recorder is the systemd StateDirectory= for the unit
# (mode 2770 wsprrec:wsprrec).
_LOCK_DIR = Path(
    os.environ.get("WSPR_HOST_SLOT_DIR") or "/var/lib/wspr-recorder"
)


def _slot_path(name: str) -> Path:
    return _LOCK_DIR / f"{name}.slots"


@contextlib.contextmanager
def host_wide_slot(
    name: str,
    n_slots: int,
    *,
    timeout: float = 600.0,
) -> Generator[int, None, None]:
    """Acquire one of ``n_slots`` slots on the host-wide named semaphore.

    Yields the acquired slot index (0..n_slots-1).  Blocks until a slot
    becomes free, polling at 0.5 s intervals; raises ``TimeoutError``
    after ``timeout`` seconds (default 10 minutes — generous enough
    that a stuck slot trips it before the recorder ring eviction
    catches us).

    Multiple wspr-recorder@<id> instances coordinate via this lock.
    Release is automatic on context exit, and the kernel releases on
    process death too — no cleanup needed after a crash.
    """
    path = _slot_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o660)
    deadline = time.monotonic() + timeout
    slot: Optional[int] = None
    waited_logged = False
    try:
        while slot is None:
            for candidate in range(n_slots):
                try:
                    fcntl.lockf(
                        fd,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                        1,
                        candidate,
                    )
                    slot = candidate
                    break
                except BlockingIOError:
                    continue
            if slot is None:
                if not waited_logged:
                    logger.debug(
                        "host_wide_slot(%s): all %d slots busy; waiting",
                        name, n_slots,
                    )
                    waited_logged = True
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"host_wide_slot({name!r}): all {n_slots} slots "
                        f"busy for {timeout:.0f}s"
                    )
                time.sleep(0.5)
        yield slot
    finally:
        if slot is not None:
            try:
                fcntl.lockf(fd, fcntl.LOCK_UN, 1, slot)
            except OSError:
                pass
        os.close(fd)


def slot_for_period(period_seconds: int) -> Optional[Tuple[str, int]]:
    """Return ``(lock_name, n_slots)`` for a decode period, or ``None``.

    W2/F2 (120 s) and F5 (300 s) are left unbounded — their slices are
    small enough that simultaneous decode across all bands/instances
    doesn't exceed reasonable per-instance MemoryMax.  F15 and F30 get
    host-wide slot caps so cross-instance peaks stay bounded.
    """
    if period_seconds >= 1800:
        return "f30", F30_SLOTS
    if period_seconds >= 900:
        return "f15", F15_SLOTS
    return None
