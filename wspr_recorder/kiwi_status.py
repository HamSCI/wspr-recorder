"""Read a KiwiSDR's ``/status`` page (plain ``key=value`` lines)."""
from __future__ import annotations

import logging
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


def parse_kiwi_status(text: str) -> dict:
    """``key=value`` lines -> dict (split on the first ``=``; blank and
    ``=``-less lines are skipped)."""
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def fetch_kiwi_status(host: str, port: int,
                      timeout: float = 5.0) -> Optional[dict]:
    """GET ``http://host:port/status`` and parse it; None on any error."""
    url = f"http://{host}:{port}/status"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - unreachable, timeout, HTTP error
        logger.debug("kiwi status %s: %s", url, exc)
        return None
    status = parse_kiwi_status(text)
    return status or None


def status_int(status: Optional[dict], key: str) -> Optional[int]:
    """``status[key]`` as an int, or None when absent or not an integer."""
    if not status:
        return None
    try:
        return int(status[key])
    except (KeyError, ValueError):
        return None
