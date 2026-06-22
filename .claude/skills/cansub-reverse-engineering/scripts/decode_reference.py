"""
decode_reference.py - decode a reference signal from an existing log via a DBC.

Offline counterpart to the Flask reference capture: when the user ALREADY has a
recorded webCAN CSV that contains BOTH proprietary raw CAN data AND a separately
decodable reference (OBD2 responses, a GPS-to-CAN / CANmod / sensor-to-CAN module
- anything they have a DBC for), decode the chosen reference signal straight from
the log and emit a standard sidecar CSV. The existing correlate / bitsearch /
build_dbc / verify / calibrate chain then runs UNCHANGED against the raw CAN IDs.
No bus, no Flask, no human slider - so the reference has near-zero timing lag and
usually fits far better than the hand-driven method (it may be coarser/sparser).

REFERENCE-SOURCE AGNOSTIC: we decode strictly what the supplied DBC defines for
the CAN IDs present in the log. cantools handles multiplexed DBCs (e.g. OBD2)
transparently; there is no protocol-specific logic here. Frames whose ID isn't in
the DBC are ignored - supply a DBC that covers the IDs you care about.

webCAN CSV is native to python-can-cansub; CANedge users can produce it from an
MF4 log via the mdf2csv converter.

Examples:
    # 1) list the reference signals decodable from this log (forgot which?):
    python decode_reference.py --trace log.csv --dbc OBD.dbc
    # 2) decode one into a sidecar + verification plot:
    python decode_reference.py --trace log.csv --dbc OBD.dbc --signal speed \
        --label speed_ref --out temp-output/sidecar_speed_ref.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import cantools  # noqa: E402
import common  # noqa: E402


def _num(x):
    """Coerce a cantools decoded value (float / NamedSignalValue) to float, or None."""
    try:
        return float(getattr(x, "value", x))
    except (TypeError, ValueError):
        return None


def _decode_subset(df, db, src_ids):
    """Decode every frame whose id is in src_ids -> list of (id, t, decoded_dict)."""
    msgs = {i: db.get_message_by_frame_id(i) for i in src_ids}
    sub = df[df["id"].isin(src_ids)]
    out = []
    for t, cid, data in zip(sub["t"].to_numpy(), sub["id"].to_numpy(), sub["data"]):
        try:
            dec = msgs[int(cid)].decode(bytes(data), allow_truncated=True,
                                        decode_choices=False)
        except Exception:
            continue
        out.append((int(cid), float(t), dec))
    return out


def _unit_of(db, src_ids, name) -> str:
    for i in src_ids:
        try:
            sig = db.get_message_by_frame_id(i).get_signal_by_name(name)
            if sig.unit:
                return sig.unit
        except Exception:
            pass
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True, help="recorded webCAN CSV (raw + reference)")
    ap.add_argument("--dbc", required=True, help="DBC that decodes the reference source")
    ap.add_argument("--signal", help="reference signal (name or case-insensitive "
                    "substring); omit to LIST decodable signals and stop")
    ap.add_argument("--label", help="sidecar label (default = resolved signal name)")
    ap.add_argument("--out", help="sidecar CSV (default temp-output/sidecar_<label>.csv)")
    ap.add_argument("--png", help="verification plot (default temp-output/decode_<label>.png)")
    ap.add_argument("--ids", help="restrict to these DBC source IDs (hex csv); "
                    "default = all DBC message IDs present in the log")
    args = ap.parse_args()

    try:
        df = common.load_trace(args.trace)
    except Exception as exc:  # noqa: BLE001 - surface a clear format hint
        print(f"ERROR: could not load '{args.trace}' as a webCAN CSV ({exc}).\n"
              f"This offline workflow expects webCAN format (header "
              f"'TimestampEpoch;BusChannel;ID;IDE;DLC;DataLength;Dir;EDL;BRS;ESI;RTR;"
              f"DataBytes'). CANedge users: convert MF4->CSV with mdf2csv first.",
              file=sys.stderr)
        return 1
    db = cantools.database.load_file(args.dbc)

    trace_ids = {int(i) for i in df["id"].unique()}
    dbc_ids = {m.frame_id for m in db.messages}
    src_ids = sorted(trace_ids & dbc_ids)
    if args.ids:
        want = {int(x, 0) for x in args.ids.split(",")}
        src_ids = [i for i in src_ids if i in want]
    if not src_ids:
        sample = sorted(hex(i) for i in dbc_ids)[:8]
        print(f"ERROR: none of the DBC's message IDs appear in {args.trace}. Wrong DBC "
              f"or wrong log? (DBC defines e.g. {sample})", file=sys.stderr)
        return 1

    decoded = _decode_subset(df, db, src_ids)
    if not decoded:
        print(f"ERROR: could not decode any frame from IDs "
              f"{[hex(i) for i in src_ids]} (empty/short frames or DBC mismatch).",
              file=sys.stderr)
        return 1

    # per-signal stats across the decoded reference frames
    stats: dict[str, dict] = {}
    for cid, _t, dec in decoded:
        for name, val in dec.items():
            v = _num(val)
            if v is None:
                continue
            s = stats.get(name)
            if s is None:
                stats[name] = {"count": 1, "ids": {cid}, "min": v, "max": v, "sum": v}
            else:
                s["count"] += 1
                s["ids"].add(cid)
                s["min"] = min(s["min"], v)
                s["max"] = max(s["max"], v)
                s["sum"] += v

    # LIST mode -------------------------------------------------------------
    if not args.signal:
        print(f"Reference signals decodable from {args.trace} with "
              f"{Path(args.dbc).name}\n(source IDs {[hex(i) for i in src_ids]}):\n")
        print(f"  {'signal':<34}{'n':>7}  {'unit':<8}{'min':>10}{'max':>10}"
              f"{'mean':>10}  source")
        print("  " + "-" * 86)
        for name, s in sorted(stats.items(), key=lambda kv: -kv[1]["count"]):
            mean = s["sum"] / s["count"]
            ids = ",".join(sorted(hex(i) for i in s["ids"]))
            print(f"  {name:<34}{s['count']:>7}  {_unit_of(db, src_ids, name):<8}"
                  f"{s['min']:>10.4g}{s['max']:>10.4g}{mean:>10.4g}  {ids}")
        print("\nPass --signal <name-or-substring> (e.g. --signal speed) to decode one "
              "into a sidecar.")
        return 0

    # resolve --signal: case-insensitive exact, else substring. When a substring
    # matches several, prefer the one with FAR more samples (the real data signal,
    # not a one-shot like an OBD2 "supported-PIDs" bitmask); only call it ambiguous
    # when the top matches have comparable sample counts.
    names = list(stats.keys())
    q = args.signal.lower()
    exact = [n for n in names if n.lower() == q]
    subs = sorted([n for n in names if q in n.lower()],
                  key=lambda n: -stats[n]["count"])
    if exact:
        target = exact[0]
    elif not subs:
        print(f"ERROR: no decodable signal matches '{args.signal}'. Present: "
              f"{', '.join(sorted(names))}", file=sys.stderr)
        return 1
    elif len(subs) == 1 or stats[subs[0]]["count"] >= 3 * stats[subs[1]]["count"]:
        target = subs[0]
        if len(subs) > 1:
            print(f"Note: '{args.signal}' matched {len(subs)} signals; selected "
                  f"{target} (n={stats[target]['count']}) over "
                  f"{subs[1]} (n={stats[subs[1]]['count']}).", file=sys.stderr)
    else:
        listing = ", ".join(f"{n} (n={stats[n]['count']})" for n in subs)
        print(f"ERROR: '{args.signal}' is ambiguous - matches {listing}. "
              f"Refine --signal.", file=sys.stderr)
        return 1

    # extract the chosen signal --------------------------------------------
    rows = []
    produced = set()
    for cid, t, dec in decoded:
        if target in dec:
            v = _num(dec[target])
            if v is not None:
                rows.append((t, v))
                produced.add(cid)
    rows.sort()
    if len(rows) < 3:
        print(f"ERROR: only {len(rows)} sample(s) of '{target}' - need >=3 to "
              f"correlate. Was it actually present/polled in this log?", file=sys.stderr)
        return 1

    ts = np.array([t for t, _ in rows])
    vs = np.array([v for _, v in rows])
    span = float(ts[-1] - ts[0])
    rate = len(ts) / span if span > 0 else 0.0
    distinct = int(len(np.unique(vs)))
    label = args.label or target
    unit = _unit_of(db, src_ids, target)
    src = ",".join(sorted(hex(i) for i in produced))

    print(f"Decoded '{target}' [{unit}] from {sorted(hex(i) for i in produced)}:")
    print(f"  n={len(vs)}  min={vs.min():.4g}  max={vs.max():.4g}  mean={vs.mean():.4g}  "
          f"span={span:.0f}s  rate={rate:.1f}Hz  distinct={distinct}")
    if distinct < 5 or rate < 1.0:
        print(f"  [!] coarse reference (distinct={distinct}, rate={rate:.1f}Hz) - "
              f"correlation may be unreliable; prefer a segment with more variation.",
              file=sys.stderr)

    out = Path(args.out) if args.out else Path(f"temp-output/sidecar_{label}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("epoch;kind;label;value\n")
        for t, v in rows:
            f.write(f"{t:.6f};value;{label};{v}\n")
    print(f"Wrote {out}  ({len(rows)} value rows)")

    png = Path(args.png) if args.png else Path("temp-output/0-decode-reference.png")
    png.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(11, 4))
    ax.step(ts - ts[0], vs, where="post", color=common.COLOR_REFERENCE)
    ax.set_xlabel("time (s)")
    ax.set_ylabel(f"{target} [{unit}]")
    ax.set_title(f"Decoded reference: {target}  (from {src})")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(png, dpi=110)
    print(f"Wrote {png}  - review this to confirm the reference looks right.")

    print(f"\nNext: search the RAW bus, EXCLUDING the reference source so it can't "
          f"self-match:\n"
          f"  python scripts/survey.py --trace {args.trace} --exclude-ids {src}\n"
          f"  python scripts/correlate.py --trace {args.trace} --sidecar {out} "
          f"--type continuous --exclude-ids {src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
