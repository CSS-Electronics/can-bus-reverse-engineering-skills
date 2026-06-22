"""
verify.py - decode a trace with a DBC and GATE the result (PASS / UNCONFIRMED).

Decodes the trace with the given DBC and judges whether the field is really
correct, rather than just printing a number. It:
  * lag-aligns the reference before scoring (absorbs human reaction delay),
  * scores agreement SEPARATELY on PARKED (steady) vs MOVING segments and flags
    divergence - "matches when parked but not in motion" is the classic signature
    of a scrambled / partial field that was calibrated through a few hold points,
  * runs a reference-FREE self-consistency check (the decoded series must be
    smooth and wrap-free) so a wrong geometry is caught even with a poor
    reference,
  * emits an explicit PASS / UNCONFIRMED verdict + recommended next action, and a
    matching exit code (0 = PASS, 2 = UNCONFIRMED) so it can gate a pipeline.

Example:
    python verify.py --trace temp-output/trace_run.csv \
        --dbc temp-output/VehicleSpeed.dbc --sidecar temp-output/sidecar_run.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

import cantools  # noqa: E402
import common  # noqa: E402
from common import (LAG_WINDOW_S, LAG_STEPS, N_WINDOWS, _windowed_spearman,
                    _windowed_spearman_signed, plausibility)  # noqa: E402

# Verdict thresholds (starting points; tune on fixtures).
PASS_SPEAR = 0.90      # overall monotone agreement to call a clean PASS
DELTA = 0.25           # steady - moving Spearman gap that flags a scrambled field
WRAP_FAIL = 0.02       # decoded wrap-rate above this = wrong geometry
PLAUS_OK = 0.50        # min reference-free plausibility for a PASS
STEADY_FRAC = 0.05     # parked if local reference ptp < this fraction of full range
STEADY_HALF_W = 0.5    # +/- window (s) for local steadiness
MIN_SEG = 30           # min frames to score a park/move segment


def _num(x) -> float:
    return float(getattr(x, "value", x))


def _decoded_raw(g, msg, sig) -> np.ndarray:
    """Sign-applied raw integer series for the signal (for plausibility).

    Decodes the raw field value via cantools (scaling=False) so the geometry is
    handled by the same path as the physical decode. The earlier hand-rolled
    big-endian reconstruction assumed byte alignment (byte=(start-7)//8,
    width=length//8) and silently produced a wrong field for any non-byte-aligned
    Motorola signal (e.g. a 14-bit field at start_bit 5 -> byte=-1, width=1),
    which poisoned the plausibility/extreme checks while the main decode was fine.
    """
    out = np.full(len(g.be_int), np.nan)
    for i, be in enumerate(g.be_int):
        payload = int(be).to_bytes(g.length, "big")
        try:
            out[i] = float(msg.decode(payload, scaling=False,
                                      allow_truncated=True)[sig.name])
        except Exception:
            out[i] = np.nan
    return out


def _local_ptp(t: np.ndarray, y: np.ndarray, half_w: float) -> np.ndarray:
    """Per-sample peak-to-peak of y over a +/- half_w (s) time window."""
    n = len(t)
    out = np.full(n, np.nan)
    for i in range(n):
        a = np.searchsorted(t, t[i] - half_w)
        b = np.searchsorted(t, t[i] + half_w)
        seg = y[a:b]
        seg = seg[np.isfinite(seg)]
        if seg.size:
            out[i] = float(seg.max() - seg.min())
    return out


def _best_lag(sample_fn, t, vals, given_lag, max_lag):
    if given_lag is not None:
        return float(given_lag)
    best, best_lag = -1.0, 0.0
    for lag in np.linspace(-max_lag, max_lag, LAG_STEPS):
        ref_at = sample_fn(t, lag)
        s = _windowed_spearman(ref_at, vals, N_WINDOWS)
        if s > best:
            best, best_lag = s, float(lag)
    return best_lag


def _window_spans(t, decoded, spans, lag, rng):
    """For each sampling window, return (t0_rel, t1_rel, value, steady, ptp) where
    `steady` flags whether the DECODED signal actually held still inside the window
    (i.e. you sampled steady state, not a transition). ptp is the decoded
    peak-to-peak in the window; steady = ptp <= STEADY_FRAC of the reference range.
    """
    out = []
    for a0, a1, v in spans:
        s0, s1 = a0 + lag, a1 + lag
        m = (t >= s0) & (t <= s1) & np.isfinite(decoded)
        ptp = float(np.ptp(decoded[m])) if m.any() else float("nan")
        steady = bool(m.any() and ptp <= STEADY_FRAC * rng)
        out.append((float(s0 - t[0]), float(s1 - t[0]), float(v), steady, ptp))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--dbc", required=True)
    ap.add_argument("--sidecar", help="optional human reference overlay")
    ap.add_argument("--lag", type=float, help="apply this reference lag (s); else auto-search")
    ap.add_argument("--max-lag", type=float, default=LAG_WINDOW_S,
                    help="+/- lag auto-search window (s)")
    ap.add_argument("--png", help="plot path (default temp-output/verify_<signal>.png)")
    ap.add_argument("--drop-extreme", action="store_true",
                    help="exclude maxed-out / out-of-band sentinel samples "
                         "(e.g. 0xFFFF 'value unavailable') from scoring and the plot")
    ap.add_argument("--ref-window", type=float,
                    help="holds windows-only: score ONLY the fixed window (s) after each "
                         "anchor tag; the full signal is still decoded/plotted, but only "
                         "deliberately-held steady data counts as the reference")
    ap.add_argument("--ref-guard", type=float, default=0.0,
                    help="seconds to skip at the start of each --ref-window (click jitter)")
    args = ap.parse_args()

    db = cantools.database.load_file(args.dbc)
    msg = db.messages[0]
    sig = msg.signals[0]
    signal_name = sig.name
    unit = sig.unit or ""

    df = common.load_trace(args.trace)
    groups = common.group_by_id(df)
    if msg.frame_id not in groups:
        print(f"ERROR: no frames for ID 0x{msg.frame_id:X} in trace.", file=sys.stderr)
        return 1
    g = groups[msg.frame_id]

    t = g.t
    vals = []
    for be in g.be_int:
        payload = int(be).to_bytes(g.length, "big")
        try:
            dec = msg.decode(payload, allow_truncated=True)
            vals.append(_num(dec[signal_name]))
        except Exception:
            vals.append(np.nan)
    vals = np.array(vals, dtype=np.float64)
    finite = np.isfinite(vals)
    if finite.sum() == 0:
        print("ERROR: could not decode any frame.", file=sys.stderr)
        return 1

    print(f"Signal '{signal_name}'  ({finite.sum()} samples, {unit})")
    print(f"  min={np.nanmin(vals):.4g}  max={np.nanmax(vals):.4g}  "
          f"mean={np.nanmean(vals):.4g}  std={np.nanstd(vals):.4g}")

    # --- extreme-outlier (maxed-out sentinel) handling ---
    # Keep two views: `vals` (sentinels intact, for the PNG + sample rows) and
    # `vals_score`/`raw_score` (sentinels masked, for scoring + verdict). At HIGH
    # confidence we exclude sentinels from SCORING even without --drop-extreme
    # ("gate on the clean subset") so a correctly-located field can't false-FAIL
    # purely on kept sentinels (their engine-off jumps trip wrap_rate). The
    # --drop-extreme flag additionally removes them from the plot.
    raw = _decoded_raw(g, msg, sig)
    extreme = common.detect_extreme_outliers(raw, sig.length, t=t)
    extreme_mask = extreme["mask"] if extreme else None
    high_conf = bool(extreme and extreme.get("confidence") == "high")
    vals_score = vals.copy()
    raw_score = raw.copy()
    if extreme:
        print("  [!] EXTREME OUTLIERS: "
              + common.describe_extreme_outliers(
                  extreme, sig.length, scale=sig.scale, offset=sig.offset, unit=unit))
        if args.drop_extreme or high_conf:
            vals_score[extreme_mask] = np.nan
            raw_score[extreme_mask] = np.nan
            if args.drop_extreme:
                vals = vals.copy(); vals[extreme_mask] = np.nan   # also off the plot
                print(f"      --drop-extreme: excluding {extreme['count']} sample(s) "
                      f"from scoring and the plot.")
            else:
                print(f"      [i] gate computed on the clean subset "
                      f"({extreme['count']} high-confidence sentinel frame(s) "
                      f"excluded from scoring; still shown on the plot). Pass "
                      f"--drop-extreme to also remove them from the plot.")
        else:
            print("      not high-confidence, so kept in scoring; re-run with "
                  "--drop-extreme to exclude them (ask the user first).")
    finite = np.isfinite(vals_score)

    # --- reference-free self-consistency (always available) ---
    # plausibility needs the per-frame series WITHOUT NaN gaps (dropped extremes),
    # else NaNs poison its mean/diff; removing a few isolated frames is harmless.
    plaus = plausibility(raw_score[np.isfinite(raw_score)], sig.length)
    print(f"  self-consistency: plausibility={plaus['score']:.2f}  "
          f"wrap_rate={plaus['wrap_rate']:.3f}  jump_rate={plaus['jump_rate']:.3f}")

    spear_overall = spear_steady = spear_moving = None
    ref_t = ref_v = None
    moving_mask = None
    window_spans = None
    best_lag = 0.0
    if args.sidecar:
        sidecar = common.load_sidecar(args.sidecar)
        sampler = common.make_reference_sampler(
            sidecar, window=args.ref_window, guard=args.ref_guard)
        if not sampler.windowed:
            ref_t, ref_v = common.continuous_reference(sidecar)
        have_ref = (len(sampler.spans) >= 2) if sampler.windowed else (len(ref_t) >= 3)
        if have_ref:
            best_lag = _best_lag(sampler, t, vals, args.lag, args.max_lag)
            ref_at = sampler(t, best_lag)
            valid = np.isfinite(ref_at) & finite
            if valid.sum() >= MIN_SEG and np.ptp(vals_score[valid]) > 0:
                spear_overall, _ = _windowed_spearman_signed(
                    ref_at[valid], vals_score[valid], N_WINDOWS)
                if sampler.windowed:
                    # windows-only: every scored sample is a deliberate steady hold,
                    # so all valid -> "parked"; the motion between holds is unlabelled
                    # and validated reference-free (plausibility), not scored here.
                    rng = float(np.nanmax(ref_at[valid]) - np.nanmin(ref_at[valid])) or 1.0
                    parked = valid
                    moving = np.zeros_like(valid)
                    window_spans = _window_spans(t, vals, sampler.spans, best_lag, rng)
                else:
                    # park vs move by local reference steadiness
                    rng = float(np.nanmax(ref_v) - np.nanmin(ref_v)) or 1.0
                    lp = _local_ptp(t, ref_at, STEADY_HALF_W)
                    parked = valid & np.isfinite(lp) & (lp < STEADY_FRAC * rng)
                    moving = valid & ~parked & np.isfinite(lp)
                    moving_mask = moving
                if parked.sum() >= MIN_SEG and np.ptp(vals_score[parked]) > 0:
                    spear_steady, _ = _windowed_spearman_signed(
                        ref_at[parked], vals_score[parked], N_WINDOWS)
                if moving.sum() >= MIN_SEG and np.ptp(vals_score[moving]) > 0:
                    spear_moving, _ = _windowed_spearman_signed(
                        ref_at[moving], vals_score[moving], N_WINDOWS)
                # shape residual after affine refit (calibration-independent)
                a, b = np.polyfit(vals_score[valid], ref_at[valid], 1)
                resid = ref_at[valid] - (a * vals_score[valid] + b)
                nrmse = float(np.sqrt(np.mean(resid ** 2)) / rng)
                msg_lag = f"lag={best_lag:+.2f}s" if args.lag is None else f"lag={best_lag:+.2f}s (given)"
                print(f"  vs reference ({msg_lag}):  Spearman_overall="
                      f"{spear_overall:.3f}  norm_resid={nrmse:.3f}")
                if window_spans is not None:
                    nbad = sum(1 for *_, st, _p in window_spans if not st)
                    print(f"    windows  Spearman={_fmt(spear_steady)} "
                          f"({len(window_spans)} sampled, n={int(parked.sum())})"
                          + (f"   [!] {nbad} window(s) NOT steady - a transition "
                             "leaked into the sample" if nbad else "   all steady"))
                else:
                    print(f"    parked  Spearman={_fmt(spear_steady)} (n={int(parked.sum())})"
                          f"   moving Spearman={_fmt(spear_moving)} (n={int(moving.sum())})")
                # ABSOLUTE agreement (NOT affine-refit, so a wrong scale/offset is
                # NOT hidden): mean signed bias + decoded-vs-reference slope (~1 ideal).
                mean_bias = float(np.mean(vals_score[valid] - ref_at[valid]))
                slope = float(np.polyfit(ref_at[valid], vals_score[valid], 1)[0])
                bias_flag = abs(mean_bias) > 0.02 * rng or abs(slope - 1.0) > 0.02
                print(f"    abs. agreement: mean bias {mean_bias:+.3g} {unit} "
                      f"({100 * mean_bias / rng:+.1f}% of range), slope {slope:.3f}"
                      + ("  [!] systematic bias - check scale / reference bias"
                         if bias_flag else ""))

    # --- verdict ---
    verdict, action = _verdict(spear_overall, spear_steady, spear_moving, plaus,
                               bool(args.sidecar))
    mark = "PASS" if verdict == "PASS" else "UNCONFIRMED"
    print(f"\n  VERDICT: {mark}")
    print(f"  -> {action}")

    # sample rows for the LLM
    print("\n  t_rel(s)   decoded")
    idx = np.linspace(0, len(vals) - 1, min(10, len(vals))).astype(int)
    for i in idx:
        print(f"  {t[i]-t[0]:8.2f}  {vals[i]:10.4g}")

    out = Path(args.png) if args.png else Path(f"temp-output/verify_{signal_name}.png")
    ref_t_rel = (ref_t + best_lag - t[0]) if ref_t is not None else None
    common.plot_reference_overlay(
        out, f"{signal_name} - decoded vs reference (same axis)",
        t - t[0], vals, unit=unit, ref_t_rel=ref_t_rel, ref_v=ref_v,
        decoded_label=f"decoded {signal_name}", reference_label="reference",
        moving_mask=moving_mask, extreme_mask=extreme_mask, window_spans=window_spans)
    print(f"\nWrote {out}")
    return 0 if verdict == "PASS" else 2


def _fmt(x):
    return "  n/a" if x is None else f"{x:.3f}"


def _verdict(spear_overall, spear_steady, spear_moving, plaus, have_ref):
    """Return (verdict, recommended_action)."""
    if plaus["wrap_rate"] > WRAP_FAIL:
        return ("UNCONFIRMED",
                "decoded series WRAPS at a field/byte boundary (wrap_rate "
                f"{plaus['wrap_rate']:.2f}) - wrong start-bit / length / endianness. "
                "Re-run bitsearch.")
    if (spear_steady is not None and spear_moving is not None
            and (spear_steady - spear_moving) > DELTA):
        return ("UNCONFIRMED",
                "matches when PARKED but not in MOTION - likely a scrambled / partial "
                "field calibrated through a few hold points. Request a full-range "
                "excitation run (e.g. shake/sweep) and re-run bitsearch.")
    if not have_ref:
        if plaus["score"] >= PLAUS_OK:
            return ("PASS", "no reference supplied; decoded series is physically "
                    "self-consistent (smooth, wrap-free). Supply --sidecar to confirm "
                    "against the real-world reference.")
        return ("UNCONFIRMED", "no reference and the decoded series is not "
                "self-consistent (jumpy/wrapping). Re-check geometry.")
    if spear_overall is not None and spear_overall >= PASS_SPEAR and plaus["score"] >= PLAUS_OK:
        return ("PASS", "decode tracks the reference across steady and moving "
                "segments and is physically self-consistent.")
    if (spear_overall is not None and spear_overall >= PASS_SPEAR
            and plaus["score"] < PLAUS_OK):
        return ("UNCONFIRMED",
                f"tracks the reference (Spearman {_fmt(spear_overall)}) but the decoded "
                f"series is not self-consistent (plausibility {plaus['score']:.2f} < "
                f"{PLAUS_OK}; jumpy/wrapping) - re-check geometry (bitsearch).")
    return ("UNCONFIRMED",
            f"correlation below threshold (Spearman {_fmt(spear_overall)} < "
            f"{PASS_SPEAR}). Reconsider geometry (re-run bitsearch) or capture a "
            "cleaner / fuller reference run.")


if __name__ == "__main__":
    raise SystemExit(main())
