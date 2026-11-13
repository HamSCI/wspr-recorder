"""hs-uploader-driven wspr upload shim.

In-process uploader for wspr-recorder.  It took over the role of the
standalone ``wd-upload-hs@.service`` in v3 Phase A (wsprdaemon-client
dissolution).

One pipeline runs inside one ``Uploader``:

  * **wsprdaemon-tar (cycle)** — ``WsprCycleSource`` on
    ``(wspr.spots, wspr.noise)`` plus the cycle's ``psk.spots`` rows →
    ``WsprdaemonTarSftp`` → wsprdaemon.org via SFTP.  Yields one tar per
    WSPR 2-min cycle containing parallel ``wsprdaemon/spots/...`` and
    ``wsprdaemon/noise/...`` subtrees, matching the legacy v3 server's
    expected tar layout.  Tar filename is cycle-time-derived
    (``YYMMDD_HHMM``) so concurrent pumps can't race on rename.

Uniform routing (sigmond, 2026-11-13): every spot goes only to
wsprdaemon.org, which stores it and forwards copies to wsprnet.org and
pskreporter.info under the spot's own reporter id.  The tar transport
therefore sets ``forward_to_wsprnet=True``, and each psk row carries its
own ``forward_to_pskreporter`` flag (True unless the producer opted
out), which the tar builder collects into ``routing.json``.  The shim
sends no reporter identity, and it builds no pipeline that posts to
wsprnet.org itself.  sigmond's ``uploader_manifest.apply_proxying`` does
the same for the standalone hs-uploader daemon.

Feature flag ``WSPR_USE_HS_UPLOADER=1`` gates the uploader.  Off (or
unset) → uploader does not start; the operator is presumably running a
different shipping path (legacy ``wd-upload-*`` chain, an external
uploader, or no uploads at all).  Matches the pre-Phase-A standalone
shim's contract so existing env files keep working.

Lifecycle (matches ``psk_recorder.core.hs_uploader_shim`` for
operational symmetry):
    start()   — construct pipelines, spawn pump thread + optional
                verifier.
    stop()    — signal stop, kick wake, join thread, stop verifier,
                close transports.
    is_active — True iff the pump thread is alive.
    wake()    — set the pump's wake Event so the next iteration
                fires immediately; wired by ``WsprRecorder.run`` to
                ``CycleBatcher``'s wake callback so every cycle
                commit fires us within milliseconds instead of
                waiting out a PUMP_INTERVAL_SEC polling tick.

WD_VERIFY_FLUSH: optional verify-and-flush thread polls wsprnet for
this reporter's spots (which wsprdaemon.org forwarded there) and deletes
confirmed rows from the local ``pending_uploads`` queue.  It only reads
wsprnet.  Off by default; opt in with WD_VERIFY_FLUSH=1.  See
``wsprnet_verifier.py`` for the full rationale.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Pump cadence.  Wake-driven (cross-rx sync wake_callback fires the
# pump the instant ALL rx commit a cycle); polling is now a long
# safety-net for the rare wake-loss case.  Pre-2026-05-21 default was
# 60 s polling — but that fired the pump BETWEEN rx commits when one
# rx was slow (e.g., a remote receiver's audio packets arriving with
# higher RTT than the local rx888mk2), splitting a single cycle into
# two POSTs.  v3's design waits for "all decoders idle" before
# uploading; our wake_callback is the equivalent event, so trust it
# and let polling only catch the truly-stuck case.
#
# ``WSPR_PUMP_INTERVAL_SEC`` env var overrides; default 300 s = 5 min
# polling backstop.  Cross-rx wake fires within ~2.5 min of cycle
# start, so wake almost always wins.
import os as _os
PUMP_INTERVAL_SEC = float(
    _os.environ.get("WSPR_PUMP_INTERVAL_SEC", "300").strip() or "300"
)

# Default sigmond sink location (matches storage_migrate.SINK_DB_PATH).
DEFAULT_SINK_DB = "/var/lib/sigmond/sink.db"


class WsprUploaderHs:
    """Pump the wspr sink → wsprdaemon.org via hs-uploader.

    wsprdaemon.org forwards the spots to wsprnet.org and
    pskreporter.info; this class posts to neither.

    Constructed from environment variables already set by wd-ctl in
    /etc/wsprdaemon/env/wd-upload-*.env — same identity inputs the
    legacy uploader uses, so swapping one for the other doesn't
    require any operator config changes beyond setting the feature
    flag.
    """

    def __init__(
        self,
        *,
        call: str,
        grid: str,
        wsprdaemon_dir: Optional[Path],
        sftp_servers: list[str],
        sftp_user: Optional[str] = None,
        upload_id: Optional[str] = None,
        version: str = "4.0",
        sink_db: Optional[Path] = None,
        instance_name: str = "",
        merge_reporters: Optional[set] = None,
        merge_backstop_sec: float = 90.0,
    ) -> None:
        self._call = call
        self._grid = grid
        # Event-driven cross-RX merge gate.  When non-empty, both upload
        # pipelines hold a WSPR cycle until every listed reporter's
        # per-cycle wspr.noise completion rows are present (or the cycle
        # is past the backstop).  Empty → single-receiver behaviour.
        self._merge_reporters = set(merge_reporters or ())
        self._merge_backstop_sec = float(merge_backstop_sec)
        self._wsprdaemon_dir = Path(wsprdaemon_dir) if wsprdaemon_dir else None
        self._sftp_servers = list(sftp_servers)
        self._sftp_user = sftp_user
        self._upload_id = upload_id
        self._version = version
        self._sink_db = Path(sink_db) if sink_db else Path(DEFAULT_SINK_DB)
        self._instance_name = instance_name or call.replace("/", "_")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._uploader = None
        self._verifier = None       # set in start() if WD_VERIFY_FLUSH=1
        self._wd_verifier = None    # set in start() if WSPRDAEMON_VERIFY=1
        self._wake_listener = None  # cross-process wake datagram listener
        self._transports: list = []
        # Per-pump per-pipeline tallies; the on_batch_outcome callback
        # populates these so the journal log line reflects what
        # actually shipped this cycle.  Reset at the top of each loop
        # iteration.
        self._pump_wsprdaemon_records = 0
        self._total_wsprdaemon_records = 0
        self._pump_count = 0
        self._work_count = 0
        # PSK (FT8/FT4) counts now ride in the per-cycle wsprdaemon tar.
        # These ACCUMULATE across pumps and are reset only when a wspr
        # cycle line is emitted (a psk-only pump keeps accumulating).
        self._psk_ft8_pending = 0
        self._psk_ft4_pending = 0
        # Per-pump scratch: the wsprdaemon tar byte size shipped this
        # pump, and the wspr cycle datetime captured from the records.
        self._pump_tar_bytes = 0
        self._pump_wspr_cycle = None

    # ----- lifecycle -----

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "WsprUploaderHs":
        """Build from the wd-ctl-generated env vars.

        Required:
            WD_RECEIVER_CALL           e.g. AC0G/B1
            WD_RECEIVER_GRID           e.g. EM38ww
        Optional:
            WD_UPLOAD_WSPRDAEMON_DIR   wsprdaemon.org spool root
            WD_SFTP_SERVERS            user@host,user@host,...
            WD_SFTP_SERVER             single user@host (legacy fallback)
            WD_SFTP_USER               override SFTP user
            WD_UPLOAD_ID               tar name prefix
            WD_VERSION                 embedded in tar config (default 4.0)
            SIGMOND_SQLITE_PATH        sink db path (default /var/lib/sigmond/sink.db)
        """
        e = env if env is not None else os.environ
        # WD_RECEIVER_CALL stays authoritative when set; the site-wide
        # STATION_REPORTER_ID (coordination.toml [station] reporter_id,
        # HamSCI/sigmond#38) is the fallback so one identity covers all
        # upload paths by default.
        call = (
            (e.get("WD_RECEIVER_CALL") or "").strip()
            or (e.get("STATION_REPORTER_ID") or "").strip()
        )
        grid = (e.get("WD_RECEIVER_GRID") or "").strip()
        if not call or not grid:
            raise ValueError(
                "wspr-uploader-hs: WD_RECEIVER_CALL and "
                "WD_RECEIVER_GRID are required"
            )
        wsprdaemon_dir = e.get("WD_UPLOAD_WSPRDAEMON_DIR") or None
        # SFTP servers: WD_SFTP_SERVERS preferred, WD_SFTP_SERVER as
        # legacy single-entry fallback — matches wd-upload-wsprdaemon.
        servers_raw = e.get("WD_SFTP_SERVERS") or e.get("WD_SFTP_SERVER") or ""
        sftp_servers = [
            s.strip() for s in servers_raw.split(",") if s.strip()
        ]
        sink_db = e.get("SIGMOND_SQLITE_PATH") or None
        # WD_MERGE_REPORTERS: comma-separated rx_call list (e.g.
        # "AC0G/B4,AC0G/B5,AC0G/B6") enabling event-driven cross-RX
        # completion gating.  Unset → single-receiver mode (no gate).
        from hs_uploader.sources.wspr_completion import parse_expected_reporters
        merge_reporters = parse_expected_reporters(e.get("WD_MERGE_REPORTERS"))
        try:
            merge_backstop = float(
                (e.get("WD_MERGE_BACKSTOP_SEC") or "90").strip() or "90"
            )
        except ValueError:
            merge_backstop = 90.0
        return cls(
            call=call, grid=grid,
            wsprdaemon_dir=wsprdaemon_dir,
            sftp_servers=sftp_servers,
            sftp_user=e.get("WD_SFTP_USER") or None,
            upload_id=e.get("WD_UPLOAD_ID") or None,
            version=e.get("WD_VERSION") or "4.0",
            sink_db=sink_db,
            instance_name=e.get("WD_INSTANCE") or "",
            merge_reporters=merge_reporters,
            merge_backstop_sec=merge_backstop,
        )

    def start(self) -> None:
        # Proper boolean parse — matches spot_sink.py and the rest of the
        # codebase.  The previous `if not env.strip()` only disabled on
        # empty/unset, so WSPR_USE_HS_UPLOADER=0 (the obvious way to turn
        # the uploader OFF) was read as a non-empty string → ENABLED.
        # That made decode-only instances in a multi-RX merge fleet each
        # run their own uploader, shipping un-merged spots under their
        # per-receiver call — defeating the single-merge-uploader
        # design.  Observed on B4-100 2026-05-30: bee1/bee2
        # (set =0) were posting as AC0G/B5 and AC0G/B6 alongside B4's
        # bare-AC0G merge, three POSTs per cycle.
        if os.environ.get(
            "WSPR_USE_HS_UPLOADER", ""
        ).strip().lower() not in ("1", "true", "yes", "on"):
            logger.info(
                "wspr-uploader-hs: WSPR_USE_HS_UPLOADER not enabled "
                "(unset / 0 / false) — shim is disabled; this instance "
                "decodes into the shared sink but does not upload"
            )
            return

        try:
            from hs_uploader import Pipeline, RetryPolicy, StationIdentity, Uploader
            from hs_uploader.watermark.sqlite import (
                SqliteWatermarkStore, default_path,
            )
        except ImportError as exc:
            logger.warning(
                "wspr-uploader-hs: hs-uploader import failed: %s", exc,
            )
            return

        watermark = SqliteWatermarkStore(default_path())
        # Pick up HS_UPLOADER_SSH_KEY_FILE (and any other identity env
        # overrides) — without this, ssh_key_file defaults to
        # /etc/hs-uploader/keys/id_ed25519 regardless of operator
        # config, so the SFTP transport authenticates with the wrong
        # key against gateways that already authorized a different key
        # (e.g. /home/wsprdaemon/.ssh/id_ed25519 from a legacy install).
        identity = StationIdentity.load()
        # WD_RECEIVER_CALL / WD_RECEIVER_GRID are the canonical
        # wsprdaemon-side env names; override the StationIdentity
        # values (which read HS_UPLOADER_CALL / HS_UPLOADER_GRID) so
        # the shim's existing env contract still wins.
        identity.call = self._call
        identity.grid = self._grid
        pipelines = []

        # --- pipeline 1: wsprdaemon.org via tar/SFTP (cycle-aligned) ---
        # One tar per WSPR cycle containing parallel wsprdaemon/spots/...
        # and wsprdaemon/noise/... subtrees, matching the v3 server's
        # expected tar layout (it parses the tar contents, not the
        # filename, and extracts reporter+grid from each record's
        # RX_SITE directory).  Replaces the previous two-pipeline design
        # (spots + noise as separate SqliteSources) which raced on the
        # SFTP filename — both pipelines pumping within the same
        # second produced two ``.tbz``s with the same upload-time name
        # and the second's rename collided with the first's already-
        # ingested file.
        wsprd_pipe = self._build_wsprdaemon_cycle_pipeline(
            identity=identity, watermark=watermark,
        )
        if wsprd_pipe is not None:
            pipelines.append(wsprd_pipe[0])
            self._transports.append(wsprd_pipe[1])

        # PSK now rides in the per-cycle wsprdaemon tar via
        # WsprCycleSource(include_psk=True); no separate psk pipeline.
        # The _build_psk_tar_pipeline method is left defined (dead but
        # harmless) in case the separate path is ever needed again.
        #
        # No wsprnet.org pipeline.  Under uniform routing (sigmond,
        # 2026-11-13) wsprdaemon.org forwards every spot to wsprnet.org;
        # the tar above carries forward_to_wsprnet=True.  A direct post
        # here as well would double-post.

        if not pipelines:
            logger.warning(
                "wspr-uploader-hs: no pipelines could be constructed "
                "(check WD_SFTP_SERVERS / SIGMOND_SQLITE_PATH); "
                "shim will exit"
            )
            return

        self._uploader = Uploader(
            pipelines,
            on_batch_outcome=self._on_batch_outcome,
        )

        self._stop.clear()
        # Wake event: pump loop waits on this OR a PUMP_INTERVAL_SEC
        # timeout, whichever comes first.  WsprRecorder.run wires
        # CycleBatcher's wake callback to our public wake() method
        # so end-to-end latency from "decoded" to "shipped" drops
        # from up to PUMP_INTERVAL_SEC (60 s) to a few hundred ms.
        # Falls back to plain polling if no callback is registered.
        self._wake = threading.Event()

        # Optional: verify-and-flush thread polls wsprnet for our
        # reporter's spots (forwarded there by wsprdaemon.org) and
        # deletes confirmed rows from the local pending_uploads queue.
        # It only reads wsprnet.  Off by default; opt in with
        # WD_VERIFY_FLUSH=1.
        # See wsprnet_verifier.py for the full rationale.
        # (Note: in the upstream wsprdaemon-client shim this block had
        # landed after a return statement inside _pid_file_path() — dead
        # code that never started the verifier.  Phase A relocates it
        # to its correct home inside start().)
        if os.environ.get("WD_VERIFY_FLUSH", "").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            try:
                from .wsprnet_verifier import WsprnetVerifier
                self._verifier = WsprnetVerifier(reporter=self._call)
                self._verifier.start()
            except Exception:
                logger.exception(
                    "wspr-uploader-hs: failed to start wsprnet verifier; "
                    "continuing without it"
                )

        # Sibling verifier for the wsprdaemon.org SFTP path.  Observe-only
        # (no DELETEs from pending_uploads — the wsprdaemon transport
        # commits via the gateway SFTP ack).  Queries wd10/wd20/wd30 ClickHouse in parallel and
        # logs per-server status + an aggregate pass-complete line.
        # Off by default; opt in with WSPRDAEMON_VERIFY=1.
        try:
            from . import wsprdaemon_verifier
            self._wd_verifier = wsprdaemon_verifier.from_env(reporter=self._call)
            if self._wd_verifier is not None:
                self._wd_verifier.start()
        except Exception:
            logger.exception(
                "wspr-uploader-hs: failed to start wsprdaemon verifier; "
                "continuing without it"
            )

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="wspr-uploader-hs",
        )
        self._thread.start()

        # Cross-process wake: peer recorder processes (other receivers in
        # the fleet) send a datagram when they commit a cycle, so our pump
        # fires the instant the LAST receiver finishes — not on the next
        # polling tick.  Best-effort; a bind failure just leaves us on the
        # polling backstop.  See upload_wake.py.
        try:
            from .upload_wake import WakeListener
            self._wake_listener = WakeListener(on_wake=self.wake)
            if not self._wake_listener.start():
                self._wake_listener = None
        except Exception:
            logger.exception(
                "wspr-uploader-hs: failed to start cross-process wake "
                "listener; continuing on the polling backstop"
            )
            self._wake_listener = None

        logger.info(
            "wspr-uploader-hs started: %s/%s (%d pipeline(s), pump=%ds)",
            self._call, self._grid, len(pipelines), int(PUMP_INTERVAL_SEC),
        )

    def wake(self) -> None:
        """Trigger an immediate pump iteration.

        Wired to CycleBatcher's wake callback by WsprRecorder.run:
        every cycle that commits spots calls us, so the pump fires
        within milliseconds instead of waiting out a PUMP_INTERVAL_SEC
        polling tick.  No-op if the wake event hasn't been created
        yet (uploader not started or feature flag off).
        """
        wake = getattr(self, "_wake", None)
        if wake is not None:
            wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        # Kick the pump loop awake so it observes _stop without
        # waiting out the rest of its PUMP_INTERVAL_SEC.
        try:
            self._wake.set()
        except AttributeError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._verifier is not None:
            try:
                self._verifier.stop(timeout=timeout)
            except Exception:
                logger.exception("wspr-uploader-hs: verifier.stop failed")
        if self._wd_verifier is not None:
            try:
                self._wd_verifier.stop(timeout=timeout)
            except Exception:
                logger.exception("wspr-uploader-hs: wd_verifier.stop failed")
        if self._wake_listener is not None:
            try:
                self._wake_listener.stop(timeout=timeout)
            except Exception:
                logger.exception("wspr-uploader-hs: wake_listener.stop failed")
        for t in self._transports:
            close = getattr(t, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        if self._pump_count:
            logger.info(
                "wspr-uploader-hs stopped after %d pump(s), %d with work "
                "(total: wsprdaemon=%d, wsprnet=0 records)",
                self._pump_count, self._work_count,
                self._total_wsprdaemon_records,
            )

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ----- pump loop -----

    def _run(self) -> None:
        while not self._stop.is_set():
            # Wait for either the regular pump interval OR a SIGUSR1
            # wake-up from a producer that just committed new spots.
            # When the wake event fires before the timeout, pump
            # immediately; otherwise this is a normal polling tick.
            woke = self._wake.wait(PUMP_INTERVAL_SEC)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._pump_count += 1
                self._pump_wsprdaemon_records = 0
                # Per-pump scratch (NOT the psk accumulators — those
                # span pumps until a cycle line is emitted).
                self._pump_tar_bytes = 0
                self._pump_wspr_cycle = None
                if self._uploader is not None and self._uploader.pump():
                    self._work_count += 1
                    self._total_wsprdaemon_records += self._pump_wsprdaemon_records
                    # Only emit a per-cycle line when a WSPR cycle
                    # actually shipped this pump.  A psk-only pump (which
                    # should no longer happen now that psk rides in the
                    # cycle tar) does NOT log and does NOT reset the psk
                    # accumulators — they keep accruing for the cycle
                    # that does ship.
                    if self._pump_wsprdaemon_records > 0:
                        cycle_str = (
                            self._pump_wspr_cycle.strftime("%H:%M")
                            if self._pump_wspr_cycle else "?"
                        )
                        tar_human = _human_bytes(self._pump_tar_bytes)
                        # wsprnet=0: this shim posts nothing to
                        # wsprnet.org; wsprdaemon.org forwards the spots.
                        # The field stays because `smd watch uploads`
                        # parses the line with a regex that needs it.
                        logger.info(
                            "wspr-uploader-hs: cycle=%s shipped "
                            "wsprdaemon=%d wsprnet=0 ft8=%d ft4=%d tar=%s",
                            cycle_str,
                            self._pump_wsprdaemon_records,
                            self._psk_ft8_pending,
                            self._psk_ft4_pending,
                            tar_human,
                        )
                        # Reset psk accumulators now that the cycle they
                        # belonged to has been disclosed.
                        self._psk_ft8_pending = 0
                        self._psk_ft4_pending = 0
            except Exception:
                logger.exception(
                    "wspr-uploader-hs: unhandled error in pump loop",
                )

    def _on_batch_outcome(self, pipeline, batch, outcome) -> None:
        """Tally the wsprdaemon tar's ship counts; outcomes other than
        acked / partial_ack are skipped (those records will retry next
        pass)."""
        if outcome.kind not in ("acked", "partial_ack"):
            return
        if pipeline.name.startswith("wsprdaemon-tar"):
            # The per-cycle tar now carries BOTH wspr.spots/noise rows
            # and psk.spots (FT8/FT4) rows.  Count wspr rows toward the
            # wsprdaemon tally and psk rows toward the ft8/ft4 tallies.
            for r in batch.records:
                tbl = getattr(r, "table", "wspr.spots") or "wspr.spots"
                if tbl == "psk.spots":
                    cols = getattr(r, "columns", None) or {}
                    mode = (cols.get("mode") or "").lower()
                    if mode == "ft8":
                        self._psk_ft8_pending += 1
                    elif mode == "ft4":
                        self._psk_ft4_pending += 1
                    # other psk modes (msk144, …) are not broken out
                else:
                    # wspr.spots / wspr.noise
                    self._pump_wsprdaemon_records += 1
                    if self._pump_wspr_cycle is None:
                        rt = getattr(r, "time", None)
                        if rt is not None:
                            self._pump_wspr_cycle = rt
            # Tar wire size (0 when the transport didn't carry it).
            self._pump_tar_bytes += getattr(outcome, "n_bytes", 0)

    # ----- pipeline construction -----

    def _build_wsprdaemon_ftp_fallback(self, *, spool_root, receiver):
        """Construct an optional ``WsprdaemonTarFtp`` for SFTP-failure
        bootstrap, or return None to disable.

        Disabled when ``WD_FTP_FALLBACK=0``.  Defaults mirror the legacy
        ``wd-upload-wsprdaemon`` script: FTP listener lives on gw2 only,
        anonymous-style ``noisegraphs`` login, ``upload`` remote path.
        The transport rebuilds the tar with ``client_upload_info.txt``
        so the gateway can auto-provision SFTP for this reporter on the
        next cycle.
        """
        if os.environ.get("WD_FTP_FALLBACK", "1").strip() in ("0", "", "no", "off"):
            return None
        try:
            from hs_uploader.transports.wsprdaemon import WsprdaemonTarFtp
        except ImportError:
            return None
        ftp_servers_raw = (
            os.environ.get("WD_FTP_SERVERS")
            or os.environ.get("WD_FTP_SERVER")
            or "gw2.wsprdaemon.org"
        )
        ftp_servers = [s.strip() for s in ftp_servers_raw.split(",") if s.strip()]
        if not ftp_servers:
            return None
        ftp_password_file = os.environ.get("WD_FTP_PASSWORD_FILE") or None
        return WsprdaemonTarFtp(
            servers=ftp_servers,
            # The FTP fallback rebuilds the tar itself, so it carries
            # the same forward flag as the SFTP transport.
            forward_to_wsprnet=True,
            spool_root=spool_root,
            ftp_user=os.environ.get("WD_FTP_USER", "noisegraphs"),
            ftp_password=os.environ.get("WD_FTP_PASSWORD", "xahFie6g"),
            ftp_password_file=ftp_password_file,
            remote_path=os.environ.get("WD_FTP_PATH", "upload"),
            version=self._version,
            upload_id=self._upload_id,
            receiver=receiver,
        )

    def _build_wsprdaemon_cycle_pipeline(self, *, identity, watermark):
        """Cycle-aligned single tar per WSPR 2-min cycle.

        Reads BOTH wspr.spots and wspr.noise from sink.db via
        ``WsprCycleSource``, yields one batch per cycle, and ships
        one tar containing parallel ``wsprdaemon/spots/...`` and
        ``wsprdaemon/noise/...`` subtrees plus ``client_upload_info.txt``
        at the tar root.  Tar filename: ``{UPLOAD_ID}_YYMMDD_HHMM.tbz``
        (cycle time, not upload time) so concurrent pumps can't race
        on the SFTP rename.  Matches the legacy v3 server's tar layout.
        """
        if not self._sftp_servers:
            logger.warning(
                "wspr-uploader-hs: WD_SFTP_SERVERS unset — skipping "
                "wsprdaemon.org pipeline"
            )
            return None
        if not self._sink_db.exists():
            logger.info(
                "wspr-uploader-hs: sink.db not present at %s — skipping "
                "wsprdaemon cycle pipeline", self._sink_db,
            )
            return None
        try:
            from hs_uploader.sources.wspr_cycle import WsprCycleSource
        except ImportError as exc:
            logger.warning(
                "wspr-uploader-hs: WsprCycleSource import failed: %s "
                "— skipping wsprdaemon cycle pipeline", exc,
            )
            return None
        from hs_uploader import Pipeline, RetryPolicy
        from hs_uploader.transports.wsprdaemon import WsprdaemonTarSftp

        source = WsprCycleSource(
            db_path=self._sink_db,
            expected_reporters=self._merge_reporters,
            backstop_sec=self._merge_backstop_sec,
            # PSK (FT8/FT4) rides in the per-cycle wsprdaemon tar — the
            # source pulls this cycle's psk.spots rows into the same
            # batch, so there's no separate psk-tar pipeline.
            include_psk=True,
        )
        receiver = os.environ.get("WD_RECEIVER_NAME", "") or self._instance_name
        fallback_ftp = self._build_wsprdaemon_ftp_fallback(
            spool_root=None, receiver=receiver,
        )
        # Compression knob — defaults to bz2 during the Phase 2 rollout
        # so a producer can ship the new tar layout while the
        # wsprdaemon-server processes on wd{10,20,30} are still on a
        # pre-zstd-sniff build (it'd ReadError zstd payloads).
        # Flip to ``zstd`` once every wd has been redeployed and we've
        # confirmed it sniffs both formats.
        tar_compression = (
            os.environ.get("WSPRDAEMON_TAR_COMPRESSION", "bz2").strip().lower()
        )
        if tar_compression not in ("bz2", "zstd"):
            logger.warning(
                "WSPRDAEMON_TAR_COMPRESSION=%r is not bz2/zstd; using bz2",
                tar_compression,
            )
            tar_compression = "bz2"
        # Tar-root knob — pairs with WSPRDAEMON_TAR_COMPRESSION. The new
        # ``wspr`` root is what every Phase 2 wd will accept, but old wd
        # processes only look under ``wsprdaemon`` and would silently
        # drop spots from a ``wspr/`` tar.  Default to ``wsprdaemon``
        # during rollout — flip to ``wspr`` once every wd has been
        # restarted with the new wsprdaemon-server code.
        tar_root = (
            os.environ.get("WSPRDAEMON_TAR_ROOT", "wsprdaemon").strip().lower()
        )
        if tar_root not in ("wspr", "wsprdaemon"):
            logger.warning(
                "WSPRDAEMON_TAR_ROOT=%r is not wspr/wsprdaemon; using wsprdaemon",
                tar_root,
            )
            tar_root = "wsprdaemon"
        transport = WsprdaemonTarSftp(
            servers=[_server_host(s) for s in self._sftp_servers],
            spool_root=None,
            sftp_user=self._sftp_user,
            version=self._version,
            upload_id=self._upload_id,
            receiver=receiver,
            fallback_ftp=fallback_ftp,
            # Cycle-aligned source — give the watermark its own key
            # so it doesn't collide with raw-table pipelines (the old
            # wsprnet pipeline used watermark key "wspr.spots").
            primary_table_name="wspr.cycle",
            compression=tar_compression,
            tar_root=tar_root,
            # Uniform routing: wsprdaemon.org forwards this station's
            # WSPR spots to wsprnet.org.  Same flag sigmond's
            # uploader_manifest.apply_proxying sets for the daemon.
            # pskreporter forwarding needs no keyword: each psk row
            # carries forward_to_pskreporter (default True), and the
            # tar builder writes it into routing.json.
            #
            # ⛔ No wsprnet_reporter / psk_reporter.  The server posts
            # under each spot's own rx_sign; only a merge rewrites the
            # reporter id, and it does so on the server.
            forward_to_wsprnet=True,
        )
        pipeline = Pipeline(
            name=f"wsprdaemon-tar-{self._instance_name}",
            source=source,
            transport=transport,
            watermark=watermark,
            identity=identity,
            retry=RetryPolicy.exponential(base=2.0, cap_sec=900.0),
            batch_limit=10_000,
        )
        logger.info(
            "wspr-uploader-hs: using WsprCycleSource for wsprdaemon "
            "(sink at %s)", self._sink_db,
        )
        return (pipeline, transport)

    def _build_psk_tar_pipeline(self, *, identity, watermark):
        """PSK rows → wsprdaemon-tar (Phase 2 PSK forwarding path).

        Reads from sink.db's ``psk.spots`` queue (populated by
        psk-recorder's ChTailer) and ships via WsprdaemonTarSftp.
        The transport already accepts ``psk.spots: [2]`` and renders
        rows as JSONL under ``ft8/`` / ``ft4/`` peer subdirs at tar
        root.  Server-side ingestion (``WSPRDAEMON_INGEST_PSK=1`` on
        wd) writes into ``psk.spots`` ClickHouse table; the
        gw1-elected ``pskreporter_forwarder`` re-posts to PSKReporter
        for rows tagged ``forward_to_pskreporter=True``.

        Gated by ``PSK_VIA_WSPRDAEMON_TAR=1``.  Operator must also
        confirm wd servers are running wsprdaemon-server >= 2.27.0
        with ingest enabled; otherwise the rows pile up unused.
        """
        if not self._sftp_servers:
            logger.info(
                "wspr-uploader-hs: PSK_VIA_WSPRDAEMON_TAR set but "
                "WD_SFTP_SERVERS unset — skipping psk pipeline"
            )
            return None
        if not self._sink_db.exists():
            logger.info(
                "wspr-uploader-hs: PSK_VIA_WSPRDAEMON_TAR set but "
                "sink.db not present at %s — skipping psk pipeline",
                self._sink_db,
            )
            return None
        try:
            from hs_uploader.sources.sqlite import SqliteSource, HEALTH_NOOP
        except ImportError as exc:
            logger.warning(
                "wspr-uploader-hs: SqliteSource import failed: %s "
                "— skipping psk pipeline", exc,
            )
            return None
        from hs_uploader import Pipeline, RetryPolicy
        from hs_uploader.transports.wsprdaemon import WsprdaemonTarSftp

        # Reuse the env-var rollout knobs (compression, root) from the
        # wsprdaemon-cycle pipeline so PSK and WSPR tars use the same
        # wire format.  Separate transport instance so its watermark
        # bookkeeping stays distinct.
        tar_compression = (
            os.environ.get("WSPRDAEMON_TAR_COMPRESSION", "bz2").strip().lower()
        )
        if tar_compression not in ("bz2", "zstd"):
            tar_compression = "bz2"
        tar_root = (
            os.environ.get("WSPRDAEMON_TAR_ROOT", "wsprdaemon").strip().lower()
        )
        if tar_root not in ("wspr", "wsprdaemon"):
            tar_root = "wsprdaemon"

        receiver = os.environ.get("WD_RECEIVER_NAME", "") or self._instance_name
        # Project just the columns the tar builder + JSONL output
        # need.  Includes forward_to_pskreporter so the server's
        # routing.json synthesizer can collapse per-receiver flags.
        #
        # ``rx_source`` and ``frequency_bucket_hz`` are stamped by
        # psk-recorder (Phase A / Phase D Cut 2 of the multi-source
        # rollout) on every spot.  The wsprdaemon-tar JSONL surface
        # uses ``json.dumps(c, ...)`` over the columns dict — so
        # including them here is all that's required to ship the
        # per-rx receiver tag + dedup bucket to wsprdaemon-server.
        # Server-side consumers that don't know these fields will
        # silently ignore them (JSON forward-compat).
        sqlite_source = SqliteSource.from_env(
            database="psk", table="spots",
            accepted_schema_versions=[2],
            start_at="now",   # cold-start: skip backlog (it'll be served by direct uploader)
            delete_on_commit=False,   # shared queue; smd storage trim handles cleanup
            select_columns=[
                "time", "mode", "frequency", "snr_db", "score", "dt",
                "tx_call", "grid", "message", "host_call", "host_grid",
                "radiod_id", "instance", "processing_version",
                "forward_to_pskreporter",
                # Multi-source visibility — the per-rx receiver key
                # (``radiod:<status_address>``) and the 100 Hz freq
                # bucket used by cross-rx dedup.  Forward-compat so
                # the server can build per-rx diversity views and run
                # its own dedup if it wants.
                "rx_source",
                "frequency_bucket_hz",
            ],
        )
        if sqlite_source.health() == HEALTH_NOOP:
            logger.info(
                "wspr-uploader-hs: psk SqliteSource resolved to no-op "
                "(sink unreadable) — skipping psk pipeline"
            )
            return None

        transport = WsprdaemonTarSftp(
            servers=[_server_host(s) for s in self._sftp_servers],
            spool_root=None,
            sftp_user=self._sftp_user,
            version=self._version,
            upload_id=self._upload_id,
            receiver=receiver,
            # Reuse the FTP fallback the wsprdaemon-cycle pipeline
            # built — fine to share, just one less object.  None is
            # also OK; psk rows are recoverable from the local sink
            # via direct upload, so no FTP fallback is acceptable.
            fallback_ftp=None,
            primary_table_name="psk.spots",   # distinct watermark key
            compression=tar_compression,
            tar_root=tar_root,
        )
        pipeline = Pipeline(
            name=f"psk-tar-{self._instance_name}",
            source=sqlite_source,
            transport=transport,
            watermark=watermark,
            identity=identity,
            retry=RetryPolicy.exponential(base=2.0, cap_sec=900.0),
            batch_limit=10_000,
        )
        logger.info(
            "wspr-uploader-hs: psk-via-tar pipeline ready (sink=%s, "
            "compression=%s, tar_root=%s)",
            self._sink_db, tar_compression, tar_root,
        )
        return (pipeline, transport)


def _human_bytes(n: int) -> str:
    """Format a byte count as a compact ``{kb:.1f}kB`` string (bytes/1024).

    Used in the per-cycle ``tar=`` field of the uploader's journal line.
    """
    try:
        kb = float(n) / 1024.0
    except (TypeError, ValueError):
        kb = 0.0
    return f"{kb:.1f}kB"


def _server_host(server_spec: str) -> str:
    """Strip ``user@`` from ``user@host`` — WsprdaemonTarSftp's
    ``servers`` arg wants host only (the user comes from
    ``sftp_user_override`` or is derived from identity.call).
    """
    if "@" in server_spec:
        return server_spec.split("@", 1)[1]
    return server_spec
