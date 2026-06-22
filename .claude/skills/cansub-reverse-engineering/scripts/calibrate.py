"""
calibrate.py - derive scale/offset from known physical anchor points.

For an already-located field (ID + byte/width or start-bit/length), pin the
raw->physical line through points you *know*: park the real-world signal at a
known value, hold steady, and record the median raw there. Far more accurate for
absolute scale than fitting a hand-driven ramp - use it to fix endpoints (e.g.
"100% only reads 94%").

  * 2 anchors  -> exact line through both (typical: 0% and 100%).
  * 3+ anchors -> least-squares line (also surfaces nonlinearity via R^2).

Two ways to supply anchors (use exactly one):

  A) --point PHYS=TRACE  (repeatable): each anchor is a separate steady capture.
     python calibrate.py --id 0x2 --byte 0 --width 2 --order little \
         --point 0=temp-output/trace_cal0.csv \
         --point 100=temp-output/trace_cal100.csv \
         --name gauge1 --unit % --out decoding-output/sensor-to-can/gauge1/gauge1.dbc

  B) --sidecar + --trace: ONE live capture in which you tagged known values with
     the flask "states" widget (kind="anchor" rows). For each tag the steady raw
     is read from a window of the trace (default: the seconds just BEFORE the tag,
     since you move-then-tag).
     python calibrate.py --id 0x2 --byte 0 --width 2 --order little \
         --trace temp-output/trace_run.csv --sidecar temp-output/sidecar_run.csv \
         --window 2 --name gauge1 --unit % \
         --out decoding-output/sensor-to-can/gauge1/gauge1.dbc
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

import common


def _load_group(trace: str, can_id: int):
    """Load a trace and return the IdGroup for can_id (raises if absent)."""
    groups = common.group_by_id(common.load_trace(trace))
    if can_id not in groups:
        raise ValueError(f"ID 0x{can_id:X} not in {trace}")
    return groups[can_id]


def _field_raw(g, args) -> tuple[np.ndarray, int, int]:
    """Field raw aligned to g.t -> (raw, cantools_start, length_bits)."""
    return common.extract_field(
        g, args.order, args.signed, byte=args.byte, width=args.width,
        start_bit=args.start_bit, length_bits=args.length_bits)


def _steady_stats(vals, length, t=None, keep_extreme=False):
    """Median + ptp of a steady hold, dropping HIGH-confidence sentinel frames
    (a momentary "signal invalid" read inside an otherwise-steady window would
    corrupt the anchor). Returns (median, ptp, n_excluded)."""
    vals = np.asarray(vals, dtype=np.float64)
    n_excl = 0
    if not keep_extreme and vals.size:
        ox = common.detect_extreme_outliers(vals, length, t=t)
        if ox and ox.get("confidence") == "high" and int((~ox["mask"]).sum()) >= 1:
            vals = vals[~ox["mask"]]
            n_excl = int(ox["count"])
    if vals.size == 0:
        return float("nan"), 0.0, n_excl
    return float(np.median(vals)), float(np.ptp(vals)), n_excl


def collect_from_points(args, can_id):
    """Anchors from separate per-point steady captures (--point PHYS=TRACE)."""
    anchors, ct_start, length, payload_len = [], None, None, None
    for spec in args.point:
        if "=" not in spec:
            raise ValueError(f"--point must be PHYS=TRACE, got {spec!r}")
        phys_s, trace = spec.split("=", 1)
        phys = float(phys_s)
        g = _load_group(trace, can_id)
        raw, ct_start, length = _field_raw(g, args)
        payload_len = g.length
        med, _, nex = _steady_stats(raw, length, g.t, args.keep_extreme)
        anchors.append((phys, med))
        extra = f"  (excluded {nex} sentinel frame(s))" if nex else ""
        print(f"  anchor phys={phys:g}  raw_median={med:.1f}  (n={g.n}){extra}")
    return anchors, ct_start, length, payload_len


def collect_from_sidecar(args, can_id):
    """Anchors from kind=='anchor' tags in one live capture (--sidecar + --trace)."""
    g = _load_group(args.trace, can_id)
    raw, ct_start, length = _field_raw(g, args)
    t = g.t
    anchor_t, anchor_v = common.anchor_reference(common.load_sidecar(args.sidecar))
    if len(anchor_t) == 0:
        raise ValueError(f'no kind="anchor" rows in {args.sidecar} '
                         "(tag known values with the flask states widget first)")
    W, guard = args.window, args.guard
    anchors = []
    for ti, vi in zip(anchor_t, anchor_v):
        if args.window_mode == "before":
            mask = (t > ti - W) & (t <= ti - guard)
        elif args.window_mode == "after":
            mask = (t >= ti + guard) & (t < ti + W)
        else:  # centered
            mask = (t >= ti - W / 2) & (t <= ti + W / 2)
        n = int(mask.sum())
        if n == 0:
            print(f"  WARN anchor phys={vi:g} @ t+{ti-t[0]:.1f}s: 0 frames in "
                  f"{args.window_mode} window — skipped", file=sys.stderr)
            continue
        sub = raw[mask]
        med, ptp, nex = _steady_stats(sub, length, t[mask], args.keep_extreme)
        flag = ""
        if ptp > 2:
            flag = f"  [!] not steady (ptp={ptp:.0f})"
        elif n < 3:
            flag = "  [!] thin window"
        if nex:
            flag += f"  (excluded {nex} sentinel frame(s))"
        print(f"  anchor phys={vi:g} @ t+{ti-t[0]:.1f}s  raw_median={med:.1f}  "
              f"n={n} ptp={ptp:.0f}{flag}")
        anchors.append((vi, med))
    return anchors, ct_start, length, g.length


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True, help="CAN id, e.g. 0x2")
    ap.add_argument("--byte", type=int, help="start byte (byte-aligned field)")
    ap.add_argument("--width", type=int, help="width in bytes (byte-aligned field)")
    ap.add_argument("--start-bit", type=int, help="Intel LSB start bit (bit-level)")
    ap.add_argument("--length-bits", type=int, help="length in bits (with --start-bit)")
    ap.add_argument("--order", choices=["little", "big"], default="little")
    ap.add_argument("--signed", action="store_true")
    # mode A: separate captures
    ap.add_argument("--point", action="append", metavar="PHYS=TRACE",
                    help="anchor: physical value = steady capture CSV (repeatable)")
    # mode B: tagged anchors in one capture
    ap.add_argument("--sidecar", help="sidecar with kind=anchor tags (with --trace)")
    ap.add_argument("--trace", help="the live trace the anchors were tagged in")
    ap.add_argument("--window", type=float, default=2.0,
                    help="seconds of steady frames per tag (default 2.0)")
    ap.add_argument("--window-mode", choices=["before", "centered", "after"],
                    default="before", help="window placement around each tag")
    ap.add_argument("--guard", type=float, default=0.0,
                    help="seconds to trim at the tag instant (click jitter)")
    ap.add_argument("--name", default="Signal")
    ap.add_argument("--unit", default="")
    ap.add_argument("--no-round", action="store_true",
                    help="keep the raw fitted scale/offset (skip BOTH the auto-snap "
                         "to neat OEM values AND the decimal tidy-up)")
    ap.add_argument("--no-decimal-round", action="store_true",
                    help="keep the full float tail on scale/offset (skip only the "
                         "decimal-place tidy-up; the OEM snap still runs)")
    ap.add_argument("--round-decimals-tol", type=float,
                    default=common.ROUND_DECIMALS_TOL,
                    help="max worst-case decode bias the decimal tidy-up may "
                         "introduce, as a fraction of range (default "
                         f"{common.ROUND_DECIMALS_TOL})")
    ap.add_argument("--keep-extreme", action="store_true",
                    help="keep high-confidence sentinel frames in the anchor median "
                         "(by default they are excluded from each steady hold)")
    ap.add_argument("--ext", action="store_true", help="extended (29-bit) frame id")
    ap.add_argument("--length", type=int, help="payload length bytes (default from capture)")
    # validate the anchor line against a moving/ramp capture (catches the
    # 3-collinear-anchors trap: a few hold points fit perfectly yet the line is
    # wrong everywhere else because the field geometry is wrong).
    ap.add_argument("--validate-trace", help="a moving/ramp capture to validate the "
                    "anchor line against (with --validate-sidecar)")
    ap.add_argument("--validate-sidecar", help="continuous reference for --validate-trace")
    ap.add_argument("--out", help="DBC path (default temp-output/<name>.dbc)")
    args = ap.parse_args()

    if bool(args.point) == bool(args.sidecar):
        print("ERROR: use exactly one of --point (separate captures) or "
              "--sidecar+--trace (tagged anchors).", file=sys.stderr)
        return 1
    if args.sidecar and not args.trace:
        print("ERROR: --sidecar needs --trace (the capture the tags were in).",
              file=sys.stderr)
        return 1

    can_id = int(args.id, 0)
    try:
        if args.point:
            anchors, ct_start, length, payload_len = collect_from_points(args, can_id)
        else:
            anchors, ct_start, length, payload_len = collect_from_sidecar(args, can_id)
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if len(anchors) < 2:
        print(f"ERROR: need at least 2 usable anchors, got {len(anchors)}.",
              file=sys.stderr)
        return 1
    phys = np.array([a[0] for a in anchors], float)
    raws = np.array([a[1] for a in anchors], float)
    if np.ptp(raws) == 0:
        print("ERROR: anchors share the same raw value — pick more separated "
              "physical points.", file=sys.stderr)
        return 1

    # least-squares raw->phys line (exact for 2 points)
    scale, offset = np.polyfit(raws, phys, 1)
    fitted = scale * raws + offset
    ss_res = float(np.sum((phys - fitted) ** 2))
    ss_tot = float(np.sum((phys - phys.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    print(f"\nLine through {len(anchors)} anchor(s):  scale={scale:.6g}  "
          f"offset={offset:.6g}  R^2={r2:.4f}")
    for p, rw in anchors:
        print(f"    phys {p:g} <-> raw {rw:.1f}  -> decodes {scale*rw+offset:.3f}")

    # auto-snap to neat OEM values when clearly safe (anchors are ground truth, so
    # a noisy multi-anchor fit that lands near a round line is snapped to it)
    if not args.no_round and len(anchors) >= 2:
        rc = common.propose_round_calibration(scale, offset, raws, phys)
        if rc:
            bits = []
            if rc["scale_changed"]:
                bits.append(f"scale {scale:.6g} -> {rc['scale']:g}")
            if rc["offset_changed"]:
                bits.append(f"offset {offset:.6g} -> {rc['offset']:g}")
            if bits:
                scale, offset = rc["scale"], rc["offset"]
                print(f"  auto-round: {', '.join(bits)}  (--no-round to keep the raw fit)")

    # decimal tidy-up: drop the raw float tail on a non-OEM scale/offset, keeping
    # the fewest decimals that stays within --round-decimals-tol of range
    if not (args.no_round or args.no_decimal_round):
        dr = common.propose_decimal_round(
            float(scale), float(offset), raws, tol=args.round_decimals_tol)
        if dr:
            bits = []
            if dr["scale_changed"]:
                bits.append(f"scale {scale:.6g} -> {dr['scale']:g} "
                            f"({dr['scale_decimals']}dp)")
            if dr["offset_changed"]:
                bits.append(f"offset {offset:.6g} -> {dr['offset']:g} "
                            f"({dr['offset_decimals']}dp)")
            scale, offset = dr["scale"], dr["offset"]
            print(f"  decimals: {', '.join(bits)}  "
                  f"(bias <={100 * dr['bias_frac']:.2f}% of range; "
                  f"--no-decimal-round to keep the full tail)")

    # round-scale plausibility (warn, never block)
    sp = common.scale_plausibility(scale)
    if not sp["nice"]:
        print(f"  [!] scale {scale:.6g} is not a round OEM value (nearest "
              f"{sp['nearest']:g}, {100*sp['rel_err']:.0f}% off) - field geometry may be "
              f"wrong (sub-slice / wrong endianness). Re-check bitsearch.", file=sys.stderr)

    # validate the line against MOVING data (the 3-collinear-anchors trap)
    if args.validate_trace and args.validate_sidecar:
        try:
            vg = _load_group(args.validate_trace, can_id)
            vraw, _, _ = _field_raw(vg, args)
            vphys = scale * vraw + offset
            vref_t, vref_v = common.continuous_reference(
                common.load_sidecar(args.validate_sidecar))
            best, best_lag = -1.0, 0.0
            for lag in np.linspace(-common.LAG_WINDOW_S, common.LAG_WINDOW_S,
                                   common.LAG_STEPS):
                ra = common.sample_hold(vref_t + lag, vref_v, vg.t)
                s = common._windowed_spearman(ra, vphys, common.N_WINDOWS)
                if s > best:
                    best, best_lag = s, float(lag)
            rs = common.residual_summary(vphys, common.sample_hold(
                vref_t + best_lag, vref_v, vg.t))
            if rs:
                print(f"\nMoving-data check (lag {best_lag:+.2f}s, n={rs['n']}):  "
                      f"median={rs['median']:.3g}  p90={rs['p90']:.3g}  "
                      f"max={rs['max']:.3g}  (p90 = {100*rs['p90_frac']:.0f}% of range)")
                if r2 >= 0.999 and rs["p90_frac"] > 0.10:
                    print("  [!] anchors fit (R^2>=0.999) but the moving data deviates - "
                          "the anchors are likely collinear and under-constrain the line, "
                          "or the field geometry is wrong. Re-run bitsearch / add anchors "
                          "spanning more of the range.", file=sys.stderr)
        except (ValueError, FileNotFoundError) as exc:
            print(f"  (moving-data check skipped: {exc})", file=sys.stderr)

    if args.length:
        payload_len = args.length
    # Declared range = theoretical representable range, not the anchor span.
    tmin, tmax = common.theoretical_range(length, args.signed, float(scale), float(offset))
    db = common.make_single_signal_db(
        name=args.name, can_id=can_id, ext=args.ext, payload_len=payload_len,
        start=ct_start, length=length, order=args.order, signed=args.signed,
        scale=float(scale), offset=float(offset),
        minimum=tmin, maximum=tmax,
        message_name=f"MSG_0x{can_id:X}",
    )
    sig = db.get_message_by_frame_id(can_id).signals[0]
    if args.unit:
        sig.unit = args.unit

    out = Path(args.out) if args.out else Path(f"temp-output/{args.name}.dbc")
    out.parent.mkdir(parents=True, exist_ok=True)
    # newline="" preserves cantools' CRLF line endings verbatim; without it,
    # text-mode writing on Windows translates each \n to \r\n, yielding \r\r\n.
    out.write_text(db.as_dbc_string(), encoding="utf-8", newline="")
    print(f"\nWrote {out}  (signal '{args.name}', {args.unit})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
