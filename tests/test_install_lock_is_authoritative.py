"""install.sh must not rewrite uv.lock, or hosts stop tracking main.

⛔ HamSCI/wspr-recorder#5, filed 2026-07-02, hand-worked-around on B4 and
again on AC0G-ND 2026-09-03 before being fixed.

The upgrade path passed `uv sync --upgrade`, documented in the script as "the
equivalent of the old pip --force-reinstall".  It is not: `--upgrade`
re-resolves dependencies and REWRITES uv.lock inside the checkout.  The dirty
lock then aborts the next fast-forward::

    $ git -C /opt/git/sigmond/wspr-recorder pull --ff-only
    error: Your local changes to the following files would be overwritten
    Aborting
     M uv.lock

and makes `smd component update` skip the pull with "uncommitted changes:
uv.lock".  So a station silently stops tracking main, and the only symptom is
a component that never updates again — nothing reports it, because from the
tool's point of view it is respecting local edits nobody made.

`--reinstall` is uv's real force-reinstall: every package is rebuilt while the
lock stays authoritative and untouched.

A static check is the right shape here.  Running the installer for real would
need uv, a venv, and the sibling checkouts; the property worth protecting is
one line of flag construction, and it regressed by someone reaching for a
plausible-sounding flag.
"""
from pathlib import Path

import re

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"


def _sync_block() -> str:
    """The sync_args construction plus the uv sync call that consumes it."""
    text = INSTALL_SH.read_text()
    m = re.search(r"local sync_args=\((.|\n)*?uv sync", text)
    assert m, "sync_args construction not found — did install.sh restructure?"
    return m.group(0)


def test_the_lock_is_never_re_resolved():
    # ⛔ The regression itself.  --upgrade rewrites uv.lock; nothing in an
    # installer should modify a tracked file in the checkout it installs from.
    assert "--upgrade" not in _sync_block(), (
        "uv sync --upgrade rewrites uv.lock and silently stops the host "
        "tracking main (HamSCI/wspr-recorder#5)")


def test_frozen_applies_to_both_paths():
    block = _sync_block()
    assert "--frozen" in block, "the lock must be authoritative on every path"
    # --frozen belongs in the base args, not inside a branch, so neither a
    # fresh install nor an upgrade can resolve around it.
    base = block.split("if [[")[0]
    assert "--frozen" in base, (
        "--frozen must be unconditional; putting it in one branch is how the "
        "upgrade path escaped it")


def test_upgrade_still_forces_a_reinstall():
    # Guards the guard: the fix must not quietly turn an upgrade into a no-op.
    # The intent — rebuild every package — is preserved by --reinstall.
    assert "--reinstall" in _sync_block()


def test_the_reason_is_recorded_next_to_the_code():
    # This regressed once from a plausible-sounding flag and a comment that
    # asserted the wrong semantics. The correction stays beside the line.
    text = INSTALL_SH.read_text()
    assert "wspr-recorder#5" in text
    assert "RE-RESOLVES" in text or "re-resolves" in text
