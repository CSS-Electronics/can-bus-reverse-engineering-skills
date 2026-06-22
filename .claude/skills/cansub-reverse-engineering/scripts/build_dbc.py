"""
build_dbc.py - derive scale/offset for a candidate field and write a 1-signal DBC.

Takes a candidate (ID, byte, width, endianness) - typically the top hit from
correlate.py - plus the human reference, and derives the decoding rule. Scale and
offset are found in TWO STAGES, not jointly (mirrors the manual "iterate scale,
then offset"):
  1. SCALE  : robust slope (Theil-Sen) of reference vs raw - resists bad inputs.
  2. OFFSET : solved after scale is fixed, offset = median(reference - scale*raw).

For a discrete signal pass --scale 1 --offset 0 to skip the fit.

Example:
    python build_dbc.py --trace temp-output/trace_run.csv \
        --sidecar temp-output/sidecar_run.csv \
        --id 0x123 --byte 1 --width 2 --order little --lag 0.2 \
        --name VehicleSpeed --unit km/h
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy import stats

import common

MAX_FIT_POINTS = 3000


def fit_scale_offset(raw: np.ndarray, ref: np.ndarray):
    """Two-stage robust fit. Returns (scale, offset, r2, n)."""
    valid = np.isfinite(raw) & np.isfinite(ref)
    raw, ref = raw[valid], ref[valid]
    if len(raw) < 5 or np.ptp(raw) == 0:
        raise ValueError("not enough varying, overlapping samples to fit")

    # subsample for Theil-Sen cost
    if len(raw) > MAX_FIT_POINTS:
        idx = np.linspace(0, len(raw) - 1, MAX_FIT_POINTS).astype(int)
        raw_s, ref_s = raw[idx], ref[idx]
    else:
        raw_s, ref_s = raw, ref

    # Stage 1: scale = robust slope
    scale = float(stats.theilslopes(ref_s, raw_s).slope)
    # Stage 2: offset after scale fixed
    offset = float(np.median(ref - scale * raw))

    fitted = offset + scale * raw
    ss_res = float(np.sum((ref - fitted) ** 2))
    ss_tot = float(np.sum((ref - np.mean(ref)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return scale, offset, r2, len(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--sidecar", help="reference (omit only with --scale/--offset)")
    ap.add_argument("--id", required=True, help="CAN id, e.g. 0x123")
    ap.add_argument("--byte", type=int, help="start byte offset (byte-aligned field)")
    ap.add_argument("--width", type=int, help="field width in bytes (byte-aligned field)")
    ap.add_argument("--start-bit", type=int, help="Intel LSB start bit (bit-level field)")
    ap.add_argument("--length-bits", type=int, help="field length in bits (with --start-bit)")
    ap.add_argument("--order", choices=["little", "big"], default="little")
    ap.add_argument("--signed", action="store_true")
    ap.add_argument("--lag", type=float, default=0.0, help="reference lag (s) from correlate")
    ap.add_argument("--ref-window", type=float,
                    help="holds windows-only: fit ONLY the fixed window (s) after each anchor "
                         "tag (deliberately-held steady data); transitions are excluded")
    ap.add_argument("--ref-guard", type=float, default=0.0,
                    help="seconds to skip at the start of each --ref-window (click jitter)")
    ap.add_argument("--name", default="Signal")
    ap.add_argument("--unit", default="")
    ap.add_argument("--scale", type=float, help="override (skip fit)")
    ap.add_argument("--offset", type=float, help="override (skip fit)")
    ap.add_argument("--drop-extreme", action="store_true",
                    help="exclude maxed-out / out-of-band sentinel samples "
                         "(e.g. 0xFFFF 'value unavailable') from the fit and range")
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
                         f"{common.ROUND_DECIMALS_TOL}); smaller keeps more decimals")
    ap.add_argument("--anchor", type=float,
                    help="physical value of the reference's rest/steady state "
                         "(e.g. 0 for a speed/flow signal at rest); re-pins the "
                         "offset so that state decodes exactly to this value. Omit "
                         "to auto-detect a true-zero rest.")
    ap.add_argument("--no-anchor", action="store_true",
                    help="disable the rest/zero physical-anchor offset correction")
    ap.add_argument("--plots-dir", help="dir for the fit-diagnostic PNG (default: "
                    "<--out parent>/analysis-plots/, else temp-output/analysis-plots/)")
    ap.add_argument("--no-plots", action="store_true",
                    help="skip the fit-diagnostic PNG")
    ap.add_argument("--out", help="DBC path (default temp-output/<name>.dbc)")
    args = ap.parse_args()

    can_id = int(args.id, 0)
    df = common.load_trace(args.trace)
    groups = common.group_by_id(df)
    if can_id not in groups:
        print(f"ERROR: ID 0x{can_id:X} not in trace (or payload too long).", file=sys.stderr)
        return 1
    g = groups[can_id]

    try:
        raw, ct_start, length = common.extract_field(
            g, args.order, args.signed, byte=args.byte, width=args.width,
            start_bit=args.start_bit, length_bits=args.length_bits)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Extreme-outlier (maxed-out sentinel) detection - ask before trusting them.
    extreme = common.detect_extreme_outliers(raw, length, t=g.t)
    extreme_mask = extreme["mask"] if extreme else None
    if extreme:
        print("  [!] EXTREME OUTLIERS: "
              + common.describe_extreme_outliers(extreme, length), file=sys.stderr)
        if args.drop_extreme:
            raw = raw.copy()
            raw[extreme_mask] = np.nan
            print(f"      --drop-extreme: excluding {extreme['count']} sample(s) "
                  f"from the fit and declared range.", file=sys.stderr)
        else:
            print("      these inflate the plot/range and the declared min/max; "
                  "re-run with --drop-extreme to exclude them (ask the user first).",
                  file=sys.stderr)

    ref_at_frames = None
    rc = None
    anchored = False
    if args.scale is not None and args.offset is not None:
        scale, offset, r2, n = args.scale, args.offset, float("nan"), g.n
        print(f"Using provided scale={scale} offset={offset}")
    else:
        if not args.sidecar:
            print("ERROR: need --sidecar (or both --scale and --offset).", file=sys.stderr)
            return 1
        sidecar = common.load_sidecar(args.sidecar)
        sampler = common.make_reference_sampler(
            sidecar, window=args.ref_window, guard=args.ref_guard)
        ref_at_frames = sampler(g.t, args.lag)
        try:
            scale, offset, r2, n = fit_scale_offset(raw, ref_at_frames)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"Two-stage fit over {n} samples:")
        print(f"  stage 1  scale  = {scale:.6g}")
        print(f"  stage 2  offset = {offset:.6g}")
        print(f"  R^2 = {r2:.4f}")

        # Auto-snap to neat OEM values - but ONLY when the rounding barely moves
        # the decode (systematic-bias budget). A rounding that would inject a
        # visible bias (a non-round OEM scale, or a biased reference like
        # indicated-vs-true speed) is reported as a SUGGESTION and left for the
        # user, never applied silently. R^2 can't see this; the bias budget can.
        if not args.no_round:
            rc = common.propose_round_calibration(scale, offset, raw, ref_at_frames)
            if rc:
                bits = []
                if rc["scale_changed"]:
                    bits.append(f"scale {scale:.6g} -> {rc['scale']:g}")
                if rc["offset_changed"]:
                    bits.append(f"offset {offset:.6g} -> {rc['offset']:g}")
                if rc["auto"]:
                    scale, offset = rc["scale"], rc["offset"]
                    print(f"  auto-round: {', '.join(bits)}  "
                          f"(bias <={100 * rc['bias_frac']:.1f}% of range; "
                          f"--no-round to keep the raw fit)")
                else:
                    print(f"  [!] round candidate NOT applied ({', '.join(bits)}): would "
                          f"inject up to {100 * rc['bias_frac']:.1f}% systematic bias vs the "
                          f"reference - likely a non-round OEM scale or a biased reference "
                          f"(e.g. indicated vs true speed). Keeping the precise fit; to force "
                          f"the round line: --scale {rc['scale']:g} --offset {rc['offset']:g}.",
                          file=sys.stderr)

        # Physical-anchor correction (runs AFTER round, so it re-pins the offset of
        # the final scale). A free fit can leave the decode reading e.g. -1.6 km/h
        # while parked - R^2 fine, physically impossible. When the reference has a
        # dense rest cluster, shift the offset so that state decodes to its true
        # value (auto only for a true-zero rest, or any value the user asserts with
        # --anchor). A LARGE required shift is flagged, not applied: it means the
        # field geometry is probably wrong, not that the offset just needs nudging.
        if not args.no_anchor:
            ac = common.propose_anchor_calibration(
                scale, offset, raw, ref_at_frames, anchor_value=args.anchor)
            if ac:
                detail = (f"rest cluster (n={ac['n_rest']}, ref~{ac['ref_level']:g}) "
                          f"decodes {ac['current']:+.4g} vs anchor {ac['anchor_value']:g} "
                          f"(off {ac['delta']:+.4g}, {100 * ac['bias_frac']:.1f}% of range)")
                if ac["auto"] or args.anchor is not None:
                    scale, offset = ac["scale"], ac["offset"]
                    anchored = True
                    sc_bit = (f", scale {ac['old_scale']:.4g}->{ac['scale']:.4g}"
                              if abs(ac["scale"] - ac["old_scale"]) > 1e-9 else "")
                    print(f"  anchor: re-fit through the rest state so it reads "
                          f"{ac['anchor_value']:g} (offset ->{ac['offset']:.4g}{sc_bit}) - "
                          f"{detail}. --no-anchor to keep the raw fit.")
                else:
                    print(f"  [!] anchor NOT applied: {detail} - too large for a fit "
                          f"cleanup; the field geometry may be wrong (re-check bitsearch). "
                          f"If the rest state truly = {ac['anchor_value']:g}, force it with "
                          f"--anchor {ac['anchor_value']:g}.", file=sys.stderr)

        # Decimal tidy-up (runs LAST, on the final scale/offset). The OEM snap
        # above only fires for a near-nice value; a genuinely non-OEM scale keeps
        # its raw float tail (fit noise). Round scale AND offset to the fewest
        # decimals that moves the decode by <= --round-decimals-tol of range.
        if not (args.no_round or args.no_decimal_round):
            dr = common.propose_decimal_round(
                scale, offset, raw, tol=args.round_decimals_tol)
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

    phys = offset + scale * raw

    # round-scale plausibility (warn, never block)
    sp = common.scale_plausibility(scale)
    if not sp["nice"]:
        print(f"  [!] scale {scale:.6g} is not a round OEM value (nearest "
              f"{sp['nearest']:g}, {100 * sp['rel_err']:.0f}% off) - a non-round scale "
              f"often means the field geometry is wrong (sub-slice / wrong endianness). "
              f"Re-check bitsearch before trusting this.", file=sys.stderr)
    # moving-data residual distribution (not just a single R^2)
    if ref_at_frames is not None:
        rs = common.residual_summary(phys, ref_at_frames)
        if rs:
            print(f"  residuals vs reference: median={rs['median']:.3g}  "
                  f"p90={rs['p90']:.3g}  max={rs['max']:.3g}  "
                  f"(p90 = {100 * rs['p90_frac']:.0f}% of range)")
            if r2 >= 0.99 and rs["p90_frac"] > 0.10:
                print("  [!] fit R^2 is high but the decode deviates from the reference "
                      "over much of the range - the ramp/anchors may under-constrain the "
                      "line, or the field geometry is wrong. Verify before trusting.",
                      file=sys.stderr)
        # absolute (NON-refit) systematic bias of the FINAL calibration vs the
        # reference - residual_summary above affine-refits, which would hide it.
        vv = np.isfinite(phys) & np.isfinite(ref_at_frames)
        if int(vv.sum()) >= 5:
            mb = float(np.mean(phys[vv] - ref_at_frames[vv]))
            rng2 = float(np.ptp(ref_at_frames[vv])) or 1.0
            flag = "  [!] systematic bias" if abs(mb) > 0.02 * rng2 else ""
            print(f"  systematic bias vs reference: {mb:+.3g} "
                  f"({100 * mb / rng2:+.1f}% of range){flag}")
    # Declared range = the field's THEORETICAL representable range (from bits +
    # scale/offset), not the observed data band.
    pmin, pmax = common.theoretical_range(length, args.signed, scale, offset)
    db = common.make_single_signal_db(
        name=args.name, can_id=can_id, ext=g.ext, payload_len=g.length,
        start=ct_start, length=length, order=args.order, signed=args.signed,
        scale=scale, offset=offset,
        minimum=pmin, maximum=pmax,
        message_name=f"MSG_0x{can_id:X}",
    )
    # attach unit
    sig = db.get_message_by_frame_id(can_id).signals[0]
    if args.unit:
        sig.unit = args.unit

    out = Path(args.out) if args.out else Path(f"temp-output/{args.name}.dbc")
    out.parent.mkdir(parents=True, exist_ok=True)
    # newline="" preserves cantools' CRLF line endings verbatim; without it,
    # text-mode writing on Windows translates each \n to \r\n, yielding \r\r\n.
    out.write_text(db.as_dbc_string(), encoding="utf-8", newline="")
    print(f"\nWrote {out}  (signal '{args.name}', range "
          f"{pmin:.6g}..{pmax:.6g} {args.unit})")
    print(f"Next: verify.py --trace {args.trace} --dbc {out}"
          + (f" --sidecar {args.sidecar}" if args.sidecar else ""))

    if not args.no_plots and ref_at_frames is not None:
        if args.plots_dir:
            plots_dir = common.resolve_plots_dir(args.plots_dir)
        elif args.out:
            plots_dir = common.resolve_plots_dir(str(Path(args.out).parent / "analysis-plots"))
        else:
            plots_dir = common.resolve_plots_dir(None)
        png = plots_dir / "4-fit-diagnostic.png"
        # only overlay the round candidate when it was NOT applied (the interesting
        # case); an applied rounding already IS the fit line, and an anchor re-pin
        # has since moved the offset so the stale candidate would only confuse.
        round_show = rc if (rc and not rc["auto"] and not anchored) else None
        common.plot_fit_diagnostic(
            png, raw, ref_at_frames, scale, offset, name=args.name, unit=args.unit,
            extreme_mask=extreme_mask, round_candidate=round_show,
            r2=(r2 if np.isfinite(r2) else None))
        print(f"Wrote {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
