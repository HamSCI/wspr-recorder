#!/usr/bin/env python3
"""
wspr-ctl: Command-line interface for wspr-recorder IPC.

Query and control wspr-recorder from the command line or scripts.

Usage:
    wspr-ctl status          # Full status
    wspr-ctl health          # Quick health check
    wspr-ctl decode-health   # Which cycles the decoders did not finish in time
    wspr-ctl timing          # Timing information
    wspr-ctl bands           # List bands
    wspr-ctl band 20         # Status for specific band
    wspr-ctl config          # Configuration
    wspr-ctl ping            # Check if running
    wspr-ctl methods         # List available methods
    wspr-ctl call <method> [json_params]  # Raw method call
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .decode_health import read_event_log
from .ipc_server import IPCClient, IPCError

DEFAULT_SOCKET = "/run/wspr-recorder/control.sock"


async def call_method(socket_path: str, method: str, params: dict = None) -> dict:
    """Call an IPC method and return the result."""
    client = IPCClient(socket_path)
    return await client.call(method, params)


def format_output(data, compact: bool = False) -> str:
    """Format output data as JSON."""
    if compact:
        return json.dumps(data)
    return json.dumps(data, indent=2)


def render_decode_health(summary: dict) -> str:
    """The readable form of the ledger — the answer to "did I lose any cycles?"."""
    counts = summary.get("counts", {})
    rule = summary.get("rule", {})
    lines = [
        f"Decode health over the last {summary.get('window_hours', '?')} h",
        "    A decode which runs longer than its cycle is fine: jobs are triggered by the audio being",
        "    available, not by a clock, so a band drains a burst by itself.  These are the cases where",
        "    that did not happen:",
        f"    BEHIND    = >= {rule.get('sustain_cycles', 5)} cycles in a row started >= "
        f"{rule.get('late_s', 120):.0f}s late with no net progress: this band will not recover",
        "    KILLED    = wsprd/jt9 was killed by its timeout, so that cycle reported no spots",
        "    DROPPED   = the cycle was never decoded at all",
        "    LATE      = one decode started a cycle or more late.  Normal after the :00/:30 wave",
        "    CAUGHT_UP = a late run ended by itself, which is the design working",
        "",
        "  " + "   ".join(f"{k} {counts.get(k, 0)}"
                          for k in ("BEHIND", "KILLED", "DROPPED", "LATE", "CAUGHT_UP")),
        f"  cycles actually lost (KILLED + DROPPED): {summary.get('cycles_missed', 0)}"
        + (f"   of {summary['decodes_total']} decodes"
           if summary.get("decodes_total") else ""),
    ]

    behind = summary.get("bands_behind") or {}
    if behind:
        lines += ["", "  NOT RECOVERING right now:"]
        for band in sorted(behind):
            v = behind[band]
            lines.append(f"    {band:<8} {v['late_cycles']} late cycles over {v['minutes']:.0f} min, "
                         f"{v['first_late_s']:.0f}s -> worst {v['worst_late_s']:.0f}s behind")

    per_band = summary.get("per_band") or {}
    if not per_band:
        lines += ["", "  No band fell permanently behind or lost a cycle in this window."]
    else:
        lines += ["", "  Bands which fell permanently behind, or lost a cycle:",
                  "    %-8s %8s %8s %8s   %s" %
                  ("BAND", "BEHIND", "KILLED", "DROPPED", "WORST LATENESS")]
        for band in sorted(per_band):
            v = per_band[band]
            lines.append("    %-8s %8d %8d %8d   %.0f s (%d cycles)" % (
                band, v.get("BEHIND", 0), v.get("KILLED", 0), v.get("DROPPED", 0),
                v.get("worst_late_s", 0), v.get("worst_late_s", 0) // 120))

    last = summary.get("last_per_band") or {}
    if last:
        lines += ["", "  Where each band stood at its most recent decode:"]
        for band in sorted(last):
            e = last[band]
            flag = "   <== NOT RECOVERING" if band in behind else ""
            lines.append("    %-8s %6.0f s behind   %-5s %-9s slot %s%s" % (
                band, e.get("late_s", 0), e.get("mode", "?"), e.get("status", "?"),
                e.get("cycle_utc", "?"), flag))

    problems = summary.get("recent_problems") or []
    if problems:
        lines += ["", "  Most recent episode events:"]
        for e in problems:
            lines.append("    %s  %-9s %-5s %-3s slot=%s late=%.0fs elapsed=%.0fs %s" % (
                e.get("utc"), e.get("status"), e.get("band"), e.get("mode"),
                e.get("cycle_utc"), e.get("late_s", 0), e.get("elapsed_s", 0),
                e.get("detail", "")))
    if summary.get("event_log"):
        lines += ["", f"  Full history: {summary['event_log']}"]
    lines += ["",
              "  A BEHIND band needs more CPU or fewer modes.  LATE runs which end in CAUGHT_UP",
              "  cost nothing: that is the decoder draining a burst, as designed."]
    return "\n".join(lines)


def summarise_events(events: list, hours: float) -> dict:
    """Build the same summary shape from the persisted JSONL, so the report
    still works when the recorder is not running."""
    import time as _time
    cutoff = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() - hours * 3600))
    events = [e for e in events if e.get("utc", "") >= cutoff]
    counts: dict = {}
    per_band: dict = {}
    last: dict = {}
    for e in events:
        st = e.get("status", "?")
        counts[st] = counts.get(st, 0) + 1
        b = per_band.setdefault(e.get("band", "?"),
                                {"BEHIND": 0, "KILLED": 0, "DROPPED": 0, "LATE": 0,
                                 "CAUGHT_UP": 0, "OK": 0, "worst_late_s": 0.0})
        b[st] = b.get(st, 0) + 1
        b["worst_late_s"] = max(b["worst_late_s"], e.get("late_s", 0))
        if st != "DROPPED":
            last[e.get("band", "?")] = e
    # A band is still behind if its last episode event was BEHIND with no
    # CAUGHT_UP after it — all the log can tell us once the daemon is gone.
    behind = {}
    for band in last:
        band_events = [x for x in events if x.get("band") == band]
        episodes = [x for x in band_events
                    if x.get("status") in ("BEHIND", "CAUGHT_UP")]
        if not episodes or episodes[-1].get("status") != "BEHIND":
            continue
        # Reconstruct the open run from the log: everything after the last
        # CAUGHT_UP (or from the start if there has never been one).
        cut = 0
        for i, x in enumerate(band_events):
            if x.get("status") == "CAUGHT_UP":
                cut = i + 1
        run = [x for x in band_events[cut:] if x.get("status") in ("LATE", "BEHIND")]
        behind[band] = {
            "late_cycles": len(run),
            "minutes": 0.0,
            "first_late_s": run[0].get("late_s", 0.0) if run else 0.0,
            "worst_late_s": max((x.get("late_s", 0.0) for x in run), default=0.0),
        }
    return {
        "window_hours": hours,
        "rule": {"late_s": 120, "sustain_cycles": 5},
        "counts": counts,
        "cycles_missed": counts.get("KILLED", 0) + counts.get("DROPPED", 0),
        "bands_behind": behind,
        "per_band": {b: v for b, v in per_band.items()
                     if any(v[k] for k in ("BEHIND", "KILLED", "DROPPED"))},
        "last_per_band": last,
        "recent_problems": [e for e in events if e.get("status")
                            in ("BEHIND", "KILLED", "DROPPED", "CAUGHT_UP")][-12:],
        "source": "event log (recorder not running)",
    }


def decode_health_cmd(args) -> int:
    """`wspr-ctl decode-health`: ask the running recorder, and fall back to the
    persisted ledger when it is down — a crashed recorder is exactly when an
    operator wants to know what the decoders were doing."""
    summary = None
    try:
        summary = asyncio.run(call_method(args.socket, "decode_health",
                                          {"hours": args.hours}))
    except Exception:                                        # noqa: BLE001
        events = read_event_log()
        if not events:
            if not args.quiet:
                print("wspr-recorder is not running and no decode-health event log "
                      "was found, so there is nothing to report.")
            return 1
        summary = summarise_events(events, args.hours)
        if not args.quiet:
            print("(wspr-recorder is not answering; reading the persisted event log)\n")
    if args.json:
        print(format_output(summary, args.compact))
    else:
        print(render_decode_health(summary))
    return 2 if summary.get("cycles_missed") else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="wspr-recorder control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    
    parser.add_argument(
        "-s", "--socket",
        default=DEFAULT_SOCKET,
        help=f"IPC socket path (default: {DEFAULT_SOCKET})",
    )
    parser.add_argument(
        "-c", "--compact",
        action="store_true",
        help="Compact JSON output (single line)",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Quiet mode - only output result, no errors",
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command")
    
    # Simple commands (no arguments)
    subparsers.add_parser("status", help="Get full status")
    subparsers.add_parser("health", help="Quick health check")
    subparsers.add_parser("timing", help="Get timing information")
    subparsers.add_parser("bands", help="List configured bands")
    subparsers.add_parser("config", help="Get configuration")
    subparsers.add_parser("ping", help="Check if recorder is running")
    subparsers.add_parser("methods", help="List available IPC methods")
    
    dh_parser = subparsers.add_parser(
        "decode-health",
        help="Cycles whose decode was killed by its timeout, outran the cycle, "
             "started late, or was never decoded at all",
    )
    dh_parser.add_argument("--hours", type=float, default=24.0,
                           help="Window to summarise (default: 24)")
    dh_parser.add_argument("--json", action="store_true",
                           help="Raw JSON instead of the readable report")

    # Band status (requires band name)
    band_parser = subparsers.add_parser("band", help="Get status for specific band")
    band_parser.add_argument("band_name", help="Band name (e.g., 20, 40, 80eu)")
    
    # Raw method call
    call_parser = subparsers.add_parser("call", help="Call arbitrary IPC method")
    call_parser.add_argument("method", help="Method name")
    call_parser.add_argument("params", nargs="?", default="{}", help="JSON parameters")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    # Map commands to methods
    method_map = {
        "status": ("status", None),
        "health": ("health", None),
        "timing": ("timing", None),
        "bands": ("bands", None),
        "config": ("config", None),
        "ping": ("ping", None),
        "methods": ("list_methods", None),
    }
    
    if args.command == "decode-health":
        return decode_health_cmd(args)

    try:
        if args.command in method_map:
            method, params = method_map[args.command]
        elif args.command == "band":
            method = "band_status"
            params = {"band": args.band_name}
        elif args.command == "call":
            method = args.method
            try:
                params = json.loads(args.params) if args.params != "{}" else None
            except json.JSONDecodeError as e:
                if not args.quiet:
                    print(f"Invalid JSON params: {e}", file=sys.stderr)
                return 1
        else:
            parser.print_help()
            return 1
        
        # Make the call
        result = asyncio.run(call_method(args.socket, method, params))
        print(format_output(result, args.compact))
        
        # For health command, return non-zero if unhealthy
        if args.command == "health" and isinstance(result, dict):
            if not result.get("healthy", True):
                return 2
        
        return 0
        
    except IPCError as e:
        if not args.quiet:
            print(json.dumps({"error": {"code": e.code, "message": e.message}}))
        return 1
    except FileNotFoundError:
        if not args.quiet:
            print(json.dumps({"error": "Socket not found - is wspr-recorder running?"}))
        return 1
    except ConnectionRefusedError:
        if not args.quiet:
            print(json.dumps({"error": "Connection refused - is wspr-recorder running?"}))
        return 1
    except asyncio.TimeoutError:
        if not args.quiet:
            print(json.dumps({"error": "Timeout connecting to wspr-recorder"}))
        return 1
    except Exception as e:
        if not args.quiet:
            print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
