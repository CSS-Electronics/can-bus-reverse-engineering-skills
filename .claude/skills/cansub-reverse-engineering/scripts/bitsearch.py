"""
bitsearch.py - canonical bit-level identification of a continuous signal field.

This is the AUTHORITATIVE permutation search: for a single ID it scans every
(start_bit x length x endianness x sign) candidate against the human reference
AND a reference-free physical-plausibility check, then reports the most likely
field with a transparent decision summary (which permutation won, and why).

  * little-endian: every start x every length in [min..max] bits, restricted to
    candidates whose BOUNDARY bits actually change - so we don't grab a constant
    valid-flag / padding bit and report a half-scale field,
  * big-endian:    byte-aligned widths (the common Motorola continuous case),
  * each scored signed AND unsigned.

Ranking (overlapping candidates are clustered; the best representative is kept):
  1. linear-fit R^2 (lag-aligned)  - the true field linearly reconstructs the
     reference; a merely monotonic sub-slice does not once lag-aligned,
  2. plausibility (reference-free) - penalises a decoded series that wraps at a
     field/byte boundary or jumps like noise (catches wrong endianness/width),
  3. windowed-Spearman magnitude   - robust monotonic corroboration,
  4. PARSIMONY at equal fit        - among NESTED candidates that fit equally well
     (R^2 within a small epsilon AND no less plausible), keep the SHORTEST. This
     demotes an OVER-WIDE read that merely appended a separate CO-VARYING field
     (e.g. the second of a wheel-speed pair) or padding: those extra bits don't
     improve R^2, so the narrower field is the true one - and its scale is the
     real one, not true/256^extra. (A too-NARROW high-byte slice fits measurably
     worse, so it is NOT preferred; a wrapping low slice is caught by rule 2.)
  5. tidiness                      - OEM byte-aligned/standard-width tie-break.

Example:
    python bitsearch.py --trace temp-output/trace_run.csv \
        --sidecar temp-output/sidecar_run.csv --id 0x123 --lag 0.2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

import common
from common import (GRID_HZ, N_WINDOWS, _windowed_spearman,
                    _windowed_spearman_signed, plausibility, scale_plausibility)


# Lag-aligned linear fit  ref ~ offset + scale*raw  -> (scale, offset, r2).
# Shared with correlate via common.linear_fit_r2; thin alias keeps existing
# call sites (and bitsearch --selftest) unchanged.
_fit_line = common.linear_fit_r2


def _changing_bits(g) -> np.ndarray:
    """Boolean mask (LSB-first global bit index) of bits that change across frames."""
    nbits = g.length * 8
    mask = np.zeros(nbits, dtype=bool)
    ints = [int(v) for v in g.le_int]
    if not ints:
        return mask
    for k in range(nbits):
        bit0 = (ints[0] >> k) & 1
        for v in ints:
            if ((v >> k) & 1) != bit0:
                mask[k] = True
                break
    return mask


def _span(entry: dict) -> tuple[int, int]:
    """Global (start_bit, length_bits) span of a candidate (LSB-first index)."""
    if entry["order"] == "little":
        return entry["start_bit"], entry["length"]
    return entry["byte"] * 8, entry["length"]


def _rank_key(e: dict) -> tuple:
    """Higher is better. R^2 leads (the true field reconstructs the reference);
    then reference-free plausibility; then Spearman magnitude; then a ROUND fitted
    scale; then the LONGEST field; then OEM tidiness.

    This key only orders the best-first WALK; the OVER-WIDE-read failure mode is
    resolved in `_suppress_overlaps` by parsimony, NOT here. (scale_nice cannot
    catch it: appending whole bytes turns a true scale s into s/256^k, and if s is
    a binary fraction - e.g. 1/64 - then s/256^k = 2^-(j+8k) is STILL a binary
    fraction, so the over-wide read scores `nice` too. The length term therefore
    prefers the LONGER read on a tie, which is exactly backwards for an over-wide
    read - hence the parsimony correction during suppression.)"""
    return (round(e["r2"], 2), round(e["plaus"], 2), round(e["spear"], 2),
            int(e["scale_nice"]), e["length"], e["tidy"])


def _extra_lsbs_continue_cascade(narrow: dict, wide: dict, rates: np.ndarray,
                                 *, jump: float = 4.0, tiny: float = 1e-6) -> bool:
    """True if the bits `wide` adds BELOW `narrow` on the LSB side are genuine field
    low bits (a flip-rate cascade), not a separate appended field.

    This is the parsimony guard's discriminator. The over-wide-read failure mode the
    parsimony swap demotes is a wide read that appended a SEPARATE co-varying field
    (its bits show a flip-rate JUMP) or constant padding (rate ~ 0). But a wide field
    whose extra low bits simply CONTINUE the cascade (each lower bit toggles ~2x more,
    no jump, never constant) is the TRUE field whose dithering LSBs a noisy reference
    made look like noise - so it must NOT be demoted to the narrow slice."""
    if narrow.get("order") != "little" or wide.get("order") != "little":
        return False
    e_lsb, k_lsb = narrow["start_bit"], wide["start_bit"]
    if k_lsb >= e_lsb:                      # wide must extend below narrow on the LSB side
        return False
    for b in range(e_lsb - 1, k_lsb - 1, -1):
        if b + 1 >= len(rates) or rates[b] <= tiny:
            return False                   # constant padding -> not a cascade continuation
        if rates[b] > jump * max(rates[b + 1], tiny):
            return False                   # a flip-rate jump -> a separate field's LSB
    return True


def _suppress_overlaps(entries: list[dict], overlap_frac: float = 0.8,
                       r2_eps: float = 0.002, rates: np.ndarray | None = None) -> list[dict]:
    """Greedy non-maximum suppression so the table shows DISTINCT physical fields.

    Walk candidates best-first (by `_rank_key`); a candidate that overlaps an
    already-kept field by >=`overlap_frac` of the SHORTER span is normally
    dropped, EXCEPT for the parsimony rule below. Genuinely ADJACENT packed fields
    (e.g. accel X / Y / Z) don't overlap, so they are all preserved.

    Parsimony (the OVER-WIDE-read fix): when the incoming candidate `e` is SHORTER
    than the kept field `k` it overlaps, fits the reference essentially as well
    (`r2` within `r2_eps`) and is no less plausible, then `k` is an over-wide read
    that appended a separate co-varying field (a wheel-speed pair) or padding -
    adding those bits did not buy any R^2 - so we SWAP `e` in as the real, narrower
    field (with the real scale). A too-NARROW high-byte slice fits measurably worse
    (R^2 drops by far more than `r2_eps` at the field's true low boundary), so it
    never displaces the true field; a wrapping low slice has lower plausibility and
    is rejected by the plausibility guard. Because the walk visits the widest
    member of a nested family first, successive shorter-but-equal members displace
    it in place until the knee (true width) remains."""
    kept: list[dict] = []
    for e in sorted(entries, key=_rank_key, reverse=True):
        s0, l0 = _span(e)
        drop = False
        for i, k in enumerate(kept):
            s1, l1 = _span(k)
            inter = max(0, min(s0 + l0, s1 + l1) - max(s0, s1))
            if inter >= overlap_frac * min(l0, l1):
                if (l0 < l1 and e["r2"] >= k["r2"] - r2_eps
                        and e["plaus"] >= k["plaus"] - r2_eps):
                    # parsimony: a narrower equal-fitting field normally displaces the
                    # wider one (the over-wide read appended a separate field / padding).
                    # BUT do not demote a wider field whose extra LSBs CONTINUE the
                    # flip-rate cascade - those are genuine low bits a noisy reference
                    # made look like noise; keep the wider (true-resolution) field.
                    if not (rates is not None
                            and _extra_lsbs_continue_cascade(e, k, rates)):
                        kept[i] = e
                drop = True
                break
        if not drop:
            kept.append(e)
    return kept


def _why(winner: dict, runner_up: dict | None) -> str:
    sp = scale_plausibility(winner["scale"])
    nice = "round OEM value" if sp["nice"] else f"non-standard (nearest {sp['nearest']:g})"
    sgn = "tracks reference" if winner["corr_sign"] >= 0 else "anti-correlated (negative scale)"
    vs = f" vs {runner_up['r2']:.3f} runner-up" if runner_up else ""
    return (f"R^2={winner['r2']:.3f}{vs}; plausibility {winner['plaus']:.2f} "
            f"(wrap_rate {winner['wrap_rate']:.2f}); "
            f"{winner['order']}/{'signed' if winner['signed'] else 'unsigned'}, "
            f"scale {winner['scale']:+.4g} [{nice}], {sgn}; "
            f"active boundary bits (not a constant flag/padding bit).")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--sidecar", required=True)
    ap.add_argument("--id", required=True, help="CAN id, e.g. 0x123")
    ap.add_argument("--min-len", type=int, default=4,
                    help="min field length (bits); bounds BOTH little- and "
                         "big-endian widths")
    ap.add_argument("--max-len", type=int, default=24,
                    help="max field length (bits); bounds BOTH orders. Default 24 "
                         "excludes 32-bit big-endian reads (usually an over-wide "
                         "concatenation of two fields); raise to 32 for a genuine "
                         "32-bit signal")
    ap.add_argument("--lag", type=float, default=0.0, help="reference lag (s) from correlate")
    ap.add_argument("--lag-refine", type=float, default=0.5,
                    help="+/- lag refinement window (s)")
    ap.add_argument("--max-lag", type=float, default=None,
                    help="symmetric +/- lag search window (s) around 0; equivalent to "
                         "--lag 0 --lag-refine <max-lag>. Matches correlate/verify "
                         "--max-lag - use it for the VIDEO workflow (e.g. --max-lag 2).")
    ap.add_argument("--ref-window", type=float,
                    help="holds windows-only: score ONLY the fixed window (s) after each anchor "
                         "tag (deliberately-held steady data); transitions excluded")
    ap.add_argument("--ref-guard", type=float, default=0.0,
                    help="seconds to skip at the start of each --ref-window (click jitter)")
    ap.add_argument("--no-resolution-refine", action="store_true",
                    help="skip growing the winning field's LSB using transition data "
                         "(by default a holds-located field is refined to its true "
                         "resolution; see common.refine_field_resolution)")
    ap.add_argument("--byte-align", action="store_true",
                    help="emit the byte/word-aligned field that ENCLOSES the exercised "
                         "field across constant bits (e.g. 3|10 -> 0|16), reproducing the "
                         "canonical OEM definition. This rewrites the reported scale by a "
                         "power of two (the LSB moves); off by default - the exercised "
                         "field is the firm result and the aligned one is shown as advice.")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--json")
    ap.add_argument("--plots-dir", help="dir for the start-bit x length R^2 grid PNG "
                    "(default temp-output/analysis-plots/; pass the signal's "
                    "analysis-plots/ folder to keep all step plots together)")
    ap.add_argument("--no-plots", action="store_true", help="skip the R^2 grid PNG")
    args = ap.parse_args()
    if args.max_lag is not None:  # symmetric video-workflow alias for --lag/--lag-refine
        args.lag = 0.0
        args.lag_refine = args.max_lag

    can_id = int(args.id, 0)
    df = common.load_trace(args.trace)
    groups = common.group_by_id(df)
    if can_id not in groups:
        print(f"ERROR: ID 0x{can_id:X} not in trace.", file=sys.stderr)
        return 1
    g = groups[can_id]
    nbits = g.length * 8
    period_s = float(np.median(np.diff(g.t))) if g.n > 1 else 0.0

    sidecar = common.load_sidecar(args.sidecar)
    sampler = common.make_reference_sampler(
        sidecar, window=args.ref_window, guard=args.ref_guard)
    if sampler.windowed:
        if len(sampler.spans) < 2:
            print("ERROR: need >=2 'anchor' tags in the sidecar for windows-only "
                  "bitsearch.", file=sys.stderr)
            return 1
        ref_lo = min(s[0] for s in sampler.spans)
        ref_hi = max(s[1] for s in sampler.spans)
    else:
        ref_t, ref_v = common.continuous_reference(sidecar)
        if len(ref_t) < 3:
            print("ERROR: need >=3 'value' samples in the sidecar.", file=sys.stderr)
            return 1
        ref_lo, ref_hi = ref_t.min(), ref_t.max()

    t0 = max(df["t"].min(), ref_lo)
    t1 = min(df["t"].max(), ref_hi)
    grid = np.arange(t0, t1, 1.0 / GRID_HZ)
    # Grid resolution scales with the window so a wide video --max-lag (e.g. +/-2s)
    # isn't sampled with only 7 points (~0.67s spacing): keep ~0.3s spacing, odd
    # count so the seed lag itself is sampled. Default +/-0.5s stays at 7 points.
    n_lags = max(7, int(round(2 * args.lag_refine / 0.3)) | 1)
    lags = np.linspace(args.lag - args.lag_refine, args.lag + args.lag_refine, n_lags)
    ref_grids = {lag: sampler(grid, lag) for lag in lags}

    # Enumerate candidates. Little-endian boundaries must be ACTIVE bits so we
    # never grab a constant flag/padding bit and report a half-scale field.
    changing = _changing_bits(g)
    # Per-bit flip rate (LSB-first) - the reference-free resolution evidence, shared by
    # the parsimony cascade guard, the resolution refinement and the cascade plot.
    rates = common._bit_flip_rates_le(g.le_int, nbits)
    candidates = []
    for start in range(nbits):
        if not changing[start]:
            continue
        for length in range(args.min_len, args.max_len + 1):
            end = start + length - 1
            if end < nbits and changing[end]:
                candidates.append(("little", start, length))
    for byte in range(g.length):
        for width in range(1, min(4, g.length - byte) + 1):
            if args.min_len <= width * 8 <= args.max_len:   # honour the length bounds
                candidates.append(("big", byte, width))

    results = []
    for order, a, b in candidates:
        if order == "little":
            raw = common.extract_le(g.le_int, a, b)
            length = b
        else:
            raw = common.extract_be(g.be_int, g.length, a, b)
            length = b * 8
        if np.ptp(raw) == 0:
            continue
        for signed in (False, True):
            rr = common.apply_sign(raw, length, signed)
            if signed and np.array_equal(rr, raw):
                continue
            # Auto-mask high-confidence sentinels BEFORE scoring so a far-out
            # "signal invalid" code can't wreck this candidate's linear R^2 (the
            # metric that decides the winner). Masking only fires at high
            # confidence + <=15% (auto_mask_outliers), so a wrong field's diffuse
            # scatter is never masked away. _fit_line / plausibility / Spearman all
            # isfinite-filter, so the masked frames simply drop out.
            rr_fit, ox = common.auto_mask_outliers(rr, length, t=g.t)
            sig_grid = common.sample_hold(g.t, rr_fit, grid)
            best, best_lag = -1.0, lags[len(lags) // 2]
            for lag, rg in ref_grids.items():
                s = _windowed_spearman(rg, sig_grid, N_WINDOWS)
                if s > best:
                    best, best_lag = s, lag
            mag, sign = _windowed_spearman_signed(ref_grids[best_lag], sig_grid, N_WINDOWS)
            ref_at = sampler(g.t, best_lag)
            scale, offset, r2 = _fit_line(rr_fit, ref_at)
            plaus = plausibility(rr_fit[np.isfinite(rr_fit)], length, period_s)
            byte_aligned = (a % 8 == 0) if order == "little" else True
            entry = {
                "id_hex": f"{can_id:X}", "order": order, "signed": signed,
                "length": length, "r2": round(r2, 4), "plaus": plaus["score"],
                "wrap_rate": plaus["wrap_rate"], "spear": round(mag, 4),
                "corr_sign": sign, "scale": round(scale, 8),
                "scale_nice": scale_plausibility(scale)["nice"],
                "offset": round(offset, 6), "lag_s": round(float(best_lag), 3),
                "masked": int(ox is not None), "mask_n": int(ox["count"]) if ox else 0,
                "tidy": int(byte_aligned) + int(length % 8 == 0)
                + int(length in (8, 16, 24, 32)),
            }
            if order == "little":
                entry.update({"start_bit": a, "byte": a // 8, "bit_in_byte": a % 8})
            else:
                entry.update({"byte": a, "width": b, "start_bit": a * 8 + 7})
            results.append(entry)

    if not results:
        print("No varying candidates found.", file=sys.stderr)
        return 1

    reps = _suppress_overlaps(results, rates=rates)[:args.top]

    # Resolution refinement: a field located from sparse steady holds has a reliable
    # MSB but can be under-resolved on the LSB side (a coarse high-bit slice fits the
    # same discrete hold levels). Grow the winner LSB-ward using the reference-free
    # TRANSITION data so a high-resolution field is not mistaken for a coarse slice.
    # No-op in continuous mode (the ramps already shaped the score) and when there is
    # too little transition data. See common.refine_field_resolution.
    refine_plot = None
    if not args.no_resolution_refine and reps and reps[0]["order"] == "little":
        w0 = reps[0]
        ns, nl = common.refine_field_resolution(
            g.le_int, g.t, "little", w0["start_bit"], w0["length"], bool(w0["signed"]),
            rate=rates)
        if (ns, nl) != (w0["start_bit"], w0["length"]):
            rr = common.apply_sign(common.extract_le(g.le_int, ns, nl), nl, bool(w0["signed"]))
            rr_fit, _ = common.auto_mask_outliers(rr, nl, t=g.t)
            ref_at = sampler(g.t, w0["lag_s"])
            scale, offset, r2 = _fit_line(rr_fit, ref_at)
            pl = plausibility(rr_fit[np.isfinite(rr_fit)], nl, period_s)
            reps[0] = {**w0, "start_bit": ns, "byte": ns // 8, "bit_in_byte": ns % 8,
                       "length": nl, "r2": round(r2, 4), "plaus": pl["score"],
                       "wrap_rate": pl["wrap_rate"], "scale": round(scale, 8),
                       "scale_nice": common.scale_plausibility(scale)["nice"],
                       "offset": round(offset, 6),
                       "refined_from": f"{w0['start_bit']}|{w0['length']}"}
            # make the refined cell available to the grid so its winner box draws,
            # and stash the narrow-vs-refined decode for the dedicated 3b plot
            if not any(x["order"] == "little" and x.get("start_bit") == ns
                       and x["length"] == nl for x in results):
                results.append(reps[0])
            narrow_raw = common.apply_sign(
                common.extract_le(g.le_int, w0["start_bit"], w0["length"]),
                w0["length"], bool(w0["signed"]))
            refine_plot = (f"{w0['start_bit']}|{w0['length']}", f"{ns}|{nl}",
                           narrow_raw, rr)

    # Byte-alignment snap: the EXERCISED field (the bits that actually moved) is the firm
    # result. Propose - and, with --byte-align, apply - the byte/word-aligned field that
    # ENCLOSES it across constant bits (e.g. 3|10 -> 0|16), reproducing the canonical OEM
    # definition. Applying rewrites the reported scale by a power of two (the LSB moves),
    # so it is opt-in. See common.snap_to_boundary.
    snap = None
    w0r = reps[0]
    if w0r["order"] == "little":
        ws, wl = _span(w0r)
        win_bits = set(range(ws, ws + wl))
        occupied = set()
        for k in reps[1:]:
            s1, l1 = _span(k)
            kbits = set(range(s1, s1 + l1))
            if kbits & win_bits:     # overlaps the winner -> a mirror/sub-slice, not a
                continue             # separate field; don't let it claim our constant bits
            occupied.update(kbits)
        snap = common.snap_to_boundary(w0r["start_bit"], w0r["length"], changing,
                                       signed=bool(w0r["signed"]), occupied=occupied)
        if snap and args.byte_align:
            a_start, a_len = snap["aligned"]
            rr = common.apply_sign(common.extract_le(g.le_int, a_start, a_len), a_len,
                                   bool(w0r["signed"]))
            rr_fit, _ = common.auto_mask_outliers(rr, a_len, t=g.t)
            ref_at = sampler(g.t, w0r["lag_s"])
            scale, offset, r2 = _fit_line(rr_fit, ref_at)
            pl = plausibility(rr_fit[np.isfinite(rr_fit)], a_len, period_s)
            reps[0] = {**w0r, "start_bit": a_start, "byte": a_start // 8,
                       "bit_in_byte": a_start % 8, "length": a_len, "r2": round(r2, 4),
                       "plaus": pl["score"], "wrap_rate": pl["wrap_rate"],
                       "scale": round(scale, 8),
                       "scale_nice": common.scale_plausibility(scale)["nice"],
                       "offset": round(offset, 6),
                       "byte_aligned_from": f"{w0r['start_bit']}|{w0r['length']}"}
            if not any(x["order"] == "little" and x.get("start_bit") == a_start
                       and x["length"] == a_len for x in results):
                results.append(reps[0])

    cols = ("id_hex", "order", "start_bit", "length", "signed", "r2", "plaus",
            "spear", "scale", "lag_s")
    print(f"Top {len(reps)} distinct fields for 0x{can_id:X}:\n")
    print("  ".join(f"{c:>9}" for c in cols))
    print("-" * (11 * len(cols)))
    for r in reps:
        print("  ".join(f"{str(r[c]):>9}" for c in cols))

    out = Path(args.json) if args.json else Path(f"temp-output/bitsearch_{can_id:X}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(reps, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")

    if not args.no_plots:
        plots_dir = common.resolve_plots_dir(args.plots_dir)
        png = plots_dir / "3-bitsearch-grid.png"
        common.plot_bitsearch_grid(png, results, can_id, winner=reps[0])
        print(f"Wrote {png}")
        if refine_plot is not None:
            nlabel, rlabel, nraw, rraw = refine_plot
            png3b = plots_dir / "3b-resolution-refine.png"
            common.plot_resolution_refine(
                png3b, g.t, nraw, rraw,
                narrow_label=f"narrow slice {nlabel}", refined_label=f"refined {rlabel}",
                title=f"resolution refine - ID 0x{can_id:X}: {nlabel} staircase vs "
                      f"{rlabel} ramp")
            print(f"Wrote {png3b}")
        if reps[0]["order"] == "little":
            png3c = plots_dir / "3c-bit-cascade.png"
            ex_s, ex_l = (snap["exercised"] if snap
                          else (reps[0]["start_bit"], reps[0]["length"]))
            common.plot_bit_cascade(png3c, rates, can_id=can_id, start_bit=ex_s,
                                    length=ex_l, aligned=snap["aligned"] if snap else None)
            print(f"Wrote {png3c}")

    w = reps[0]
    if w.get("refined_from"):
        print(f"\n[resolution] grew the field {w['refined_from']} -> "
              f"{w['start_bit']}|{w['length']}: the extra low bits form a flip-rate "
              f"CASCADE (each toggles ~2x more toward the LSB) bounded by a constant or "
              f"separate-field edge, so the field is higher-resolution than a narrow "
              f"slice - even when those LSBs DITHER in a noisy live stream (which the "
              f"smoothness test alone would reject). (--no-resolution-refine to disable.)")
    print(f"\nDecision: {_why(w, reps[1] if len(reps) > 1 else None)}")
    if snap:
        a_start, a_len = snap["aligned"]
        below, above, factor = (snap["constant_below"], snap["constant_above"],
                                snap["scale_factor"])
        if w.get("byte_aligned_from"):
            print(f"[byte-align] APPLIED: emitting the byte-aligned {a_start}|{a_len} "
                  f"field (exercised was {w['byte_aligned_from']}); the reported scale was "
                  f"rewritten by 1/{factor} (the LSB moved down across constant bits) to "
                  f"the canonical form.")
        else:
            ex_s, ex_l = snap["exercised"]
            print(f"[byte-align] the EXERCISED field {ex_s}|{ex_l} is the firm result; a "
                  f"byte/word-aligned {a_start}|{a_len} encloses it across constant bits. "
                  f"Pass --byte-align to emit it (scale would become 1/{factor} of this).")
        if above:
            print(f"  [!] high bits {above} never toggled - the range was likely NOT "
                  f"fully exercised; a fuller-range sweep would confirm the top bits.")
        if below:
            print(f"  [i] low bits {below} are constant here - sub-resolution (the source "
                  f"quantizes) or padding; the physical decode is the same either way.")
    # flag a maxed-out sentinel in the winning field so the user can decide early
    if w["order"] == "little":
        wraw = common.extract_le(g.le_int, w["start_bit"], w["length"])
    else:
        wraw = common.extract_be(g.be_int, g.length, w["byte"], w["width"])
    wraw = common.apply_sign(wraw, w["length"], w["signed"])
    wx = common.detect_extreme_outliers(wraw, w["length"], t=g.t)
    if wx:
        print("  [!] EXTREME OUTLIERS in this field: "
              + common.describe_extreme_outliers(wx, w["length"])
              + " Ask the user whether to drop them; if so add --drop-extreme to "
              "build_dbc / verify.", file=sys.stderr)
        if w.get("masked"):
            print("  [i] these were auto-masked during ranking (high confidence); "
                  "the winner was chosen on the clean subset.", file=sys.stderr)
    sgn = " --signed" if w["signed"] else ""
    if w["order"] == "little":
        geom = f"--order little --start-bit {w['start_bit']} --length-bits {w['length']}"
        print(f"Best (Intel): start_bit {w['start_bit']} length {w['length']} "
              f"R^2 {w['r2']} plaus {w['plaus']}")
    else:
        geom = f"--order big --byte {w['byte']} --width {w['width']}"
        print(f"Best (Motorola): byte {w['byte']} width {w['width']} "
              f"R^2 {w['r2']} plaus {w['plaus']}")
    print(f"Next: build_dbc.py --id 0x{w['id_hex']} {geom} --lag {w['lag_s']}{sgn} "
          f"--trace {args.trace} --sidecar {args.sidecar} --name MySignal")
    return 0


def _mk(order, pos, length, r2, plaus, scale_nice=1, spear=0.99, tidy=3) -> dict:
    """Build a minimal synthetic candidate for the suppression selftest."""
    e = {"order": order, "length": length, "r2": r2, "plaus": plaus,
         "spear": spear, "scale_nice": scale_nice, "tidy": tidy}
    if order == "little":
        e["start_bit"] = pos
    else:
        e["byte"] = pos
    return e


def _selftest() -> int:
    """Validate the parsimony / over-wide-read suppression without hardware."""
    ok = True

    def kept_lengths(entries):
        return sorted(c["length"] for c in _suppress_overlaps(list(entries)))

    # (1) Over-wide read: a nested byte-0 family 8/16/24/32 bits where R^2 jumps at
    #     16 then plateaus -> the 16-bit knee must win; 24/32 demoted; 8 too coarse.
    fam = [_mk("big", 0, 8, 0.9927, 0.99, scale_nice=0),
           _mk("big", 0, 16, 0.9994, 1.00),
           _mk("big", 0, 24, 0.9993, 1.00),
           _mk("big", 0, 32, 0.9993, 1.00)]
    c1 = kept_lengths(fam)
    t1 = c1 == [16]
    ok &= t1
    print(f"  over-wide nested family -> keep {c1} (want [16]) -> {'OK' if t1 else 'FAIL'}")

    # (2) True 16-bit + an adjacent DISTINCT field must both survive.
    two = fam + [_mk("little", 28, 8, 0.987, 0.99)]
    c2 = kept_lengths(two)
    t2 = c2 == [8, 16]
    ok &= t2
    print(f"  true field + distinct field -> keep {c2} (want [8, 16]) -> {'OK' if t2 else 'FAIL'}")

    # (3) Too-narrow high byte must NOT displace the true field (fit drops > eps).
    hi = [_mk("big", 0, 8, 0.992, 0.99, scale_nice=0), _mk("big", 0, 16, 0.9994, 1.00)]
    c3 = kept_lengths(hi)
    t3 = c3 == [16]
    ok &= t3
    print(f"  high-byte slice vs true 16-bit -> keep {c3} (want [16]) -> {'OK' if t3 else 'FAIL'}")

    # (4) A wrapping low slice (equal R^2 but low plausibility) must NOT displace.
    wrap = [_mk("big", 0, 16, 0.9994, 1.00), _mk("big", 0, 8, 0.9990, 0.10, scale_nice=0)]
    c4 = kept_lengths(wrap)
    t4 = c4 == [16]
    ok &= t4
    print(f"  wrapping low slice vs true 16-bit -> keep {c4} (want [16]) -> {'OK' if t4 else 'FAIL'}")

    # (5) Scoring-level regression for the sentinel bug: the TRUE wide field carries
    #     a far-out sentinel that wrecks its raw R^2 so a coarse slice out-ranks it;
    #     auto-masking the sentinel must recover the R^2 and FLIP the winner back.
    ref = np.linspace(0, 1000, 400)
    wide = ref.copy(); wide[384:] = 65535.0            # 4% structural sentinel run
    slice_q = np.floor(ref / 350.0) * 350.0            # coarse 3-level competitor
    r2_wide_raw = _fit_line(wide, ref)[2]
    wide_m, oxw = common.auto_mask_outliers(wide, 16)
    r2_wide_masked = _fit_line(wide_m, ref)[2]
    r2_slice = _fit_line(slice_q, ref)[2]

    def _rk(r2, plaus, length):
        return _rank_key({"r2": r2, "plaus": plaus, "spear": 0.99,
                          "scale_nice": 1, "length": length, "tidy": 3})
    raw_loses = _rk(r2_wide_raw, plausibility(wide, 16)["score"], 16) < \
        _rk(r2_slice, plausibility(slice_q, 8)["score"], 8)
    masked_wins = _rk(r2_wide_masked,
                      plausibility(wide_m[np.isfinite(wide_m)], 16)["score"], 16) > \
        _rk(r2_slice, plausibility(slice_q, 8)["score"], 8)
    t5 = (oxw is not None) and raw_loses and masked_wins
    ok &= t5
    print(f"  masking flips winner (wide>slice) -> raw_loses={raw_loses} "
          f"masked_wins={masked_wins} -> {'OK' if t5 else 'FAIL'}")

    # (6) parsimony cascade guard: a WIDE field whose extra LSBs CONTINUE the flip-rate
    #     cascade must NOT be demoted to a narrow high-bit slice (the noisy-LSB fix).
    #     Without the rates the old parsimony swap still picks the narrow slice.
    rates6 = np.zeros(16)
    for k in range(3, 13):
        rates6[k] = 0.08 / (2 ** (k - 3))                  # cascade over bits 3..12
    wide6 = _mk("little", 3, 10, 0.9994, 1.00)
    narrow6 = _mk("little", 6, 7, 0.9994, 1.00)
    guard = sorted(c["length"] for c in _suppress_overlaps([wide6, narrow6], rates=rates6))
    noguard = sorted(c["length"] for c in _suppress_overlaps([wide6, narrow6]))
    t6 = guard == [10] and noguard == [7]
    ok &= t6
    print(f"  cascade guard keeps wide={guard} (want [10]), no-rates={noguard} (want [7]) "
          f"-> {'OK' if t6 else 'FAIL'}")

    # (7) the guard must NOT shield a genuine OVER-WIDE read: when the extra low bits are
    #     a SEPARATE faster field (a flip-rate JUMP), the narrow field still wins.
    rates7 = np.zeros(16)
    for k in range(8, 13):
        rates7[k] = 0.01 / (2 ** (k - 8))                  # slow field bits 8..12
    for k in range(0, 8):
        rates7[k] = 0.5                                     # fast separate field below
    wide7 = _mk("little", 0, 13, 0.9994, 1.00)
    narrow7 = _mk("little", 8, 5, 0.9994, 1.00)
    got7 = sorted(c["length"] for c in _suppress_overlaps([wide7, narrow7], rates=rates7))
    t7 = got7 == [5]
    ok &= t7
    print(f"  cascade guard allows over-wide demotion -> keep {got7} (want [5]) -> "
          f"{'OK' if t7 else 'FAIL'}")

    print("bitsearch selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    raise SystemExit(main())
