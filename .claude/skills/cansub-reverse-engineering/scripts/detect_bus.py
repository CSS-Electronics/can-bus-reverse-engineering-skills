"""
detect_bus.py - discover a CANsub, resolve the bit-rate, pick a mode, health-check.

Run this FIRST. It (1) discovers connected CANsub devices via mDNS, (2) passively
probes bit-rates (listen_only + error frames) to find the active one, (3) records
the silent/ACK PROFILE for this session, and (4) reads the device bus status to
sanity-check that the bus is healthy before you start. Writes everything to
temp-output/bus.json for the other scripts.

PROFILE (--profile) sets whether the CANsub stays silent or acknowledges frames:
  * vehicle (default) -> listen_only/SILENT. Use for an existing multi-node bus
    (car / truck / bike / machine): other ECUs provide the ACK, and we never
    disturb the bus. Safe default when unsure.
  * bench -> normal/ACK. Use for a desk test of a SINGLE node (one ECU / sensor
    module): with no other node to ACK, the CANsub must ACK or the lone node hits
    ACK errors and gets stuck retransmitting.
Bit-rate probing is always listen_only regardless (you can't ACK while cycling
candidate rates); the profile only affects later capture connections.

Examples:
    python detect_bus.py --profile vehicle        # car/truck (silent)
    python detect_bus.py --profile bench          # single ECU/sensor on a desk
    python detect_bus.py --device usb --channel 1 --profile bench
    python detect_bus.py --bitrate 1000000 --profile vehicle   # skip probing
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import common


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", help="device-id / 'usb' / 'eth' substring filter")
    ap.add_argument("--channel", type=int, help="CAN channel index (1-based)")
    ap.add_argument("--bitrate", type=int, help="skip probing, use this nominal rate")
    ap.add_argument("--data-bitrate", type=int,
                    help="FD data rate (with --bitrate; default = nominal)")
    ap.add_argument("--sample-point", type=float, default=80.0)
    ap.add_argument("--profile", choices=["vehicle", "bench"], default="vehicle",
                    help="vehicle=silent/listen_only (active multi-node bus, "
                         "default/safe); bench=normal/ACK (single ECU/sensor desk test)")
    ap.add_argument("--timeout", type=float, default=common.AUTO_DETECT_TIMEOUT_S,
                    help="listen window per attempt (s)")
    ap.add_argument("--out", default="temp-output/bus.json")
    args = ap.parse_args()

    print("Discovering CANsub via mDNS ...", file=sys.stderr)
    try:
        config = common.pick_config(args.device, args.channel)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Found channel: {config['channel']}", file=sys.stderr)

    if args.bitrate:
        attempt = {"nominal": args.bitrate,
                   "data": args.data_bitrate or args.bitrate,
                   "sample_point": args.sample_point}
        print(f"Using provided timing: {attempt}", file=sys.stderr)
    else:
        print("Auto-detecting bit-rate (passive, listen_only; classical + FD) ...",
              file=sys.stderr)
        attempt, _ = common.probe_bitrate(config, timeout_s=args.timeout)
        if attempt is None:
            print("ERROR: no valid frames at any bit-rate. Is the bus active? "
                  "Re-run, or pass --bitrate explicitly.", file=sys.stderr)
            return 1
        fd = " (CAN FD)" if attempt["data"] != attempt["nominal"] else ""
        print(f"Detected: nominal={attempt['nominal']} data={attempt['data']} "
              f"sample_point={attempt['sample_point']}{fd}", file=sys.stderr)

    listen_only = args.profile == "vehicle"
    mode = "silent/listen_only" if listen_only else "normal/ACK"
    print(f"Profile: {args.profile} -> capture mode {mode}", file=sys.stderr)

    # Bus health check (read-only REST; warn loudly, never block).
    print("Checking bus status ...", file=sys.stderr)
    status = common.device_status(config["channel"])
    if status.get("ok"):
        print(f"  state={status.get('state')} frame_rate={status.get('frame_rate')} "
              f"bus_load={status.get('bus_load')}% rx_err={status.get('rx_error_count')} "
              f"tx_err={status.get('tx_error_count')} bus_err={status.get('bus_error_count')}",
              file=sys.stderr)
    healthy, warns = common.summarize_health(status, profile=args.profile)
    if healthy:
        print("  bus looks healthy.", file=sys.stderr)
    else:
        for w in warns:
            print(f"  [!] {w}", file=sys.stderr)
        print("  (continuing - this is a warning, not a stop)", file=sys.stderr)

    out = {
        "interface": config["interface"],
        "channel": config["channel"],
        "nominal": attempt["nominal"],
        "data": attempt["data"],
        "sample_point": attempt["sample_point"],
        "profile": args.profile,
        "listen_only": listen_only,
        # back-compat aliases
        "bitrate": attempt["nominal"],
        "data_bitrate": attempt["data"],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}:")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
