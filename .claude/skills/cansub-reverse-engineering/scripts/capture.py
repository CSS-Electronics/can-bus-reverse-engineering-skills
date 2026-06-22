"""
capture.py - capture a CAN trace from a CANsub to a webCAN-format CSV.

The silent-vs-ACK mode comes from the PROFILE chosen at detect_bus.py and stored
in temp-output/bus.json:
  * vehicle profile -> SILENT (listen_only): the CANsub never transmits; other
    ECUs on the bus provide the ACK. Use on a real car/truck/bike/machine.
  * bench profile -> NORMAL (ACK): the CANsub acknowledges frames. Required for a
    SINGLE node on a desk - with no ACK a lone node hits ACK errors and gets stuck
    retransmitting (it won't update cleanly).
Override per-run with --listen-only (force silent) or --normal (force ACK).
If bus.json predates profiles, the default is SILENT (safe).

The bus is opened with error_frames=True; error frames are counted and reported
(they signal a bus problem) but are kept out of the CSV, which has no error-frame
representation. Bit-rate auto-detect (detect_bus.py) is always listen_only.

Reads device + bit-rate from temp-output/bus.json (run detect_bus.py first), or
takes them as flags.

Examples:
    python capture.py --duration 15 --label baseline
    python capture.py --duration 60 --label run --ids 0x123,0x200
    python capture.py --duration 15 --label sniff --listen-only   # force silent
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import can
import common


def parse_ids(s: str | None) -> list[int] | None:
    if not s:
        return None
    return [int(x, 0) for x in s.split(",") if x.strip()]


class _Counter(can.Listener):
    """Counts data frames (and unique IDs) and error frames separately."""

    def __init__(self):
        self.n = 0
        self.errors = 0
        self.ids: set[int] = set()

    def on_message_received(self, msg: can.Message) -> None:
        if getattr(msg, "is_error_frame", False):
            self.errors += 1
            return
        self.n += 1
        self.ids.add(msg.arbitration_id)


class _DataOnly(can.Listener):
    """Forward only non-error frames to a wrapped writer (keeps error frames out
    of the webCAN CSV, which has no representation for them)."""

    def __init__(self, writer: can.Listener):
        self.writer = writer

    def on_message_received(self, msg: can.Message) -> None:
        if not getattr(msg, "is_error_frame", False):
            self.writer.on_message_received(msg)

    def stop(self) -> None:
        self.writer.stop()


def resolve_config(bus_json: str = "temp-output/bus.json", device: str | None = None,
                   channel: int | None = None, bitrate: int | None = None,
                   data_bitrate: int | None = None) -> dict:
    """Resolve {interface, channel, nominal, data, sample_point, listen_only,
    profile} for a capture.

    Reads temp-output/bus.json (written by detect_bus.py) and applies optional
    CLI-style overrides. Used by both capture.py's main() and flask_sync.py's
    in-process capture controller so they share one source of truth. `listen_only`
    comes from the recorded profile (default True/safe if bus.json predates it).
    """
    cfg = {}
    p = Path(bus_json)
    if p.exists():
        cfg = json.loads(p.read_text(encoding="utf-8"))
    if bitrate:
        cfg["nominal"] = bitrate
        cfg["data"] = data_bitrate or bitrate
        cfg.setdefault("sample_point", 80.0)
    nominal = cfg.get("nominal", cfg.get("bitrate"))
    if nominal is None:
        raise RuntimeError("no bit-rate. Run detect_bus.py or pass --bitrate.")
    data = cfg.get("data", cfg.get("data_bitrate", nominal))
    sample_point = cfg.get("sample_point", 80.0)
    interface = cfg.get("interface")
    chan = cfg.get("channel")
    if device or channel or chan is None:
        picked = common.pick_config(device, channel)
        interface, chan = picked["interface"], picked["channel"]
    return {"interface": interface, "channel": chan, "nominal": nominal,
            "data": data, "sample_point": sample_point,
            "listen_only": cfg.get("listen_only", True),
            "profile": cfg.get("profile")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, required=True, help="seconds to capture")
    ap.add_argument("--label", default="capture", help="trace label (filename stem)")
    ap.add_argument("--out", help="output CSV (default temp-output/trace_<label>.csv)")
    ap.add_argument("--bus-json", default="temp-output/bus.json")
    ap.add_argument("--device")
    ap.add_argument("--channel", type=int)
    ap.add_argument("--bitrate", type=int)
    ap.add_argument("--data-bitrate", type=int, default=common.DEFAULT_DATA_BITRATE)
    ap.add_argument("--ids", help="comma-separated hardware ID filter, e.g. 0x123,0x200")
    mode_grp = ap.add_mutually_exclusive_group()
    mode_grp.add_argument("--listen-only", dest="mode", action="store_const",
                          const="silent", help="force SILENT (no ACK) - multi-node bus")
    mode_grp.add_argument("--normal", dest="mode", action="store_const",
                          const="ack", help="force NORMAL/ACK - single-node bench")
    args = ap.parse_args()

    # Resolve config: bus.json then CLI overrides.
    try:
        cfg = resolve_config(args.bus_json, args.device, args.channel,
                             args.bitrate, args.data_bitrate)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    nominal, data, sample_point = cfg["nominal"], cfg["data"], cfg["sample_point"]

    # Effective mode: explicit flag overrides the recorded profile (bus.json).
    if args.mode == "silent":
        listen_only = True
    elif args.mode == "ack":
        listen_only = False
    else:
        listen_only = cfg["listen_only"]

    out = Path(args.out) if args.out else Path(f"temp-output/trace_{args.label}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    can_filters = None
    ids = parse_ids(args.ids)
    if ids:
        can_filters = [{"can_id": i, "can_mask": 0x1FFFFFFF} for i in ids]

    fd = f"/{data}" if data != nominal else ""
    mode = "silent/listen_only" if listen_only else "normal/ACK"
    prof = f" [{cfg['profile']}]" if cfg.get("profile") and args.mode is None else ""
    print(f"Capturing {args.duration:.0f}s on {cfg['channel']} @ {nominal}{fd} bps "
          f"({mode}{prof}) -> {out}", file=sys.stderr)

    timing = common.make_timing(nominal, data, sample_point)
    counter = _Counter()
    with can.Bus(interface=cfg["interface"], channel=cfg["channel"],
                 timing=timing, listen_only=listen_only, error_frames=True,
                 can_filters=can_filters) as bus:
        logger = can.Logger(str(out))
        with can.Notifier([bus], [_DataOnly(logger), counter]):
            time.sleep(args.duration)

    summary = f"Captured {counter.n} frames, {len(counter.ids)} unique IDs"
    if counter.errors:
        summary += f"  [!] {counter.errors} error frames seen (bus problem?)"
    print(f"{summary} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
