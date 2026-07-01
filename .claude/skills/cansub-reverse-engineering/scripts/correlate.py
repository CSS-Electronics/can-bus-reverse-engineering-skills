"""
correlate.py - rank candidate CAN signal encodings against a human reference.

Replaces "human watches the screen to derive correlation" with a deterministic
search. For a continuous reference (slider/value ramp) it ranks candidate
(ID, byte, width, endianness) by correlation; for a discrete reference (events)
it ranks candidate (ID, bit) by change-near-event lift.

Human input is imperfect, so scoring is robust to it:
  * lag search    - slides the reference +/- a window to absorb reaction delay,
  * rank-based    - Spearman (monotonic, scale-free), tolerant of nonlinearity,
  * outlier-aware - scored over overlapping windows, aggregated by median,
  * coverage flag - candidates / segments with thin overlap are flagged.

Examples:
    python correlate.py --trace temp-output/trace_run.csv \
        --sidecar temp-output/sidecar_run.csv --type continuous
    python correlate.py --trace temp-output/trace_run.csv \
        --sidecar temp-output/sidecar_run.csv --type discrete
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

import common
from common import (GRID_HZ, LAG_WINDOW_S, LAG_STEPS, N_WINDOWS,  # noqa: F401
                    _windowed_spearman, _windowed_spearman_signed, plausibility)

# Proxy-suspect flag: a field that rank-tracks the reference this well (Spearman)
# yet whose linear R^2 falls at least PROXY_GAP below that is monotonic-but-not-linear - a
# HINT it may be a co-varying proxy (a real field has R^2 ~= Spearman). Keys on the GAP, not
# an absolute R^2, and is valid only AFTER lag/geometry/sign/saturation are ruled out.
PROXY_SPEARMAN = 0.90
PROXY_GAP = 0.20
# Co-variate discriminator: |target - best co-variate| Spearman gap needed before
# we state a lean either way; inside this band the two are too close to separate globally.
COVAR_MARGIN = 0.15


# ---------------------------------------------------------------------------
# Continuous
# ---------------------------------------------------------------------------

def _candidate_fields(group, max_width=2):
    """Yield (byte, width, order) byte-aligned candidates for a group."""
    L = group.length
    for b in range(L):
        # width 1: endianness irrelevant -> emit once as 'little'
        yield (b, 1, "little")
        for w in range(2, max_width + 1):
            if b + w <= L:
                yield (b, w, "little")
                yield (b, w, "big")


def flagged_bytes(trace_path: str) -> dict:
    """Read temp-output/survey_<label>.json (written by survey.py) and return
    {can_id: {counter byte indices}} (empty if no survey).

    Only *counter* bytes are excluded - they unambiguously aren't signals. The
    survey's checksum flag is a heuristic hint only (a wide little-endian signal's
    low byte can look like a checksum without a reference), so it is NOT excluded.
    """
    sp = common.default_survey_json(trace_path)
    if not sp.exists():
        return {}
    try:
        rows = json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for r in rows:
        bad = set(r.get("counter_bytes", []))
        if bad:
            out[int(r["id"])] = bad
    return out


def correlate_continuous(df, sidecar, ids, max_width, top, flagged=None, exclude=None,
                         ref_window=None, ref_guard=0.0, max_lag=LAG_WINDOW_S,
                         covariates=None):
    groups = common.group_by_id(df)
    if ids:
        groups = {k: v for k, v in groups.items() if k in ids}
    if exclude:
        groups = {k: v for k, v in groups.items() if k not in exclude}

    sampler = common.make_reference_sampler(sidecar, window=ref_window, guard=ref_guard)
    if sampler.windowed:
        if len(sampler.spans) < 2:
            print("ERROR: need >=2 'anchor' tags in the sidecar for windows-only "
                  "correlation.", file=sys.stderr)
            return []
        ref_lo = min(s[0] for s in sampler.spans)
        ref_hi = max(s[1] for s in sampler.spans)
        levels = len({round(s[2], 6) for s in sampler.spans})
        ref_kind = f"{len(sampler.spans)} hold window(s), {levels} distinct level(s)"
    else:
        ref_t, ref_v = common.continuous_reference(sidecar)
        if len(ref_t) < 3:
            print("ERROR: need >=3 'value' samples in the sidecar.", file=sys.stderr)
            return []
        ref_lo, ref_hi = ref_t.min(), ref_t.max()
        levels = int(np.unique(np.round(ref_v, 6)).size)
        ref_kind = f"{levels} distinct reference level(s)"

    # Sparse-reference guard: with only a few distinct levels, many fields (counters
    # included) rank-match the monotone step pattern, so the ranking is DEGENERATE
    # (many ties) and a spurious #1 can read as confident. A continuous sweep fixes it.
    if levels < 5:
        print(f"[!] sparse reference ({ref_kind}): rankings can be DEGENERATE - many "
              f"candidates (counters included) tie near the top, so don't trust a single "
              f"#1. For IDENTIFICATION capture a continuous SWEEP run (flask_sync "
              f"--mode sweep) and correlate it WITHOUT --ref-window, then confirm the "
              f"field with bitsearch.", file=sys.stderr)

    # Common grid over the overlap of trace and reference.
    t0 = max(df["t"].min(), ref_lo)
    t1 = min(df["t"].max(), ref_hi)
    if t1 - t0 < 1.0:
        print("ERROR: trace and sidecar barely overlap in time.", file=sys.stderr)
        return []
    grid = np.arange(t0, t1, 1.0 / GRID_HZ)
    lags = np.linspace(-max_lag, max_lag, LAG_STEPS)

    flagged = flagged or {}
    # Co-variate samplers: the SAME continuous-reference machinery, one per
    # supplied co-variate channel, used below to score how well each candidate field also
    # tracks the co-variates (RPM/MAP/pedal) - the deterministic proxy discriminator.
    cov_samplers = [(name, common.make_reference_sampler(sc)) for name, sc in (covariates or [])]
    skipped = 0
    results = []
    for can_id, g in groups.items():
        bad = flagged.get(can_id, ())
        for byte, width, order in _candidate_fields(g, max_width):
            if bad and any((byte + k) in bad for k in range(width)):
                skipped += 1  # field overlaps a counter byte
                continue
            if order == "little":
                raw = common.extract_le(g.le_int, byte * 8, width * 8)
            else:
                raw = common.extract_be(g.be_int, g.length, byte, width)
            if np.ptp(raw) == 0:
                continue  # static field, cannot encode a varying signal
            # auto-mask high-confidence sentinels before scoring (same agnostic
            # detector as bitsearch); high-confidence + <=15% only, so a wrong
            # byte's diffuse scatter is never masked away.
            raw_fit, _ = common.auto_mask_outliers(raw, width * 8, t=g.t)
            sig_grid = common.sample_hold(g.t, raw_fit, grid)

            # lag search: shift the reference sampling, keep best window-median rho
            best = 0.0
            best_lag = 0.0
            for lag in lags:
                ref_grid = sampler(grid, lag)
                score = _windowed_spearman(ref_grid, sig_grid, N_WINDOWS)
                if score > best:
                    best, best_lag = score, lag

            # signedness: re-score signed interpretation, keep better. If signed wins,
            # re-run the lag search on the signed read so best/best_lag (hence score,
            # proxy_suspect and the co-variate margin below) all describe the read we
            # actually keep. Otherwise a signed field's score stays the UNSIGNED value,
            # which collapses across a two's-complement zero-crossing (torque on overrun)
            # and would bias the margin against the target.
            signed = False
            raw_s = common.apply_sign(raw, width * 8, True)
            raw_s_fit, _ = common.auto_mask_outliers(raw_s, width * 8, t=g.t)
            if not np.array_equal(raw_s, raw) and np.ptp(raw_s) > 0:
                sig_s = common.sample_hold(g.t, raw_s_fit, grid)
                if _windowed_spearman(sampler(grid, best_lag), sig_s, N_WINDOWS) > best:
                    signed = True
                    best, best_lag = 0.0, 0.0
                    for lag in lags:
                        score = _windowed_spearman(sampler(grid, lag), sig_s, N_WINDOWS)
                        if score > best:
                            best, best_lag = score, lag

            # reference-free plausibility + correlation sign + linear-fit R^2 for the
            # chosen read. R^2 is scale-AWARE (Spearman is monotonic/scale-free), so it
            # orders the Spearman-tied shortlist: a coarse or wrong-scale slice that only
            # MOVES with the signal scores ~1.0 on Spearman but lower on R^2, so it can no
            # longer top genuine carriers on plausibility alone (the 0x1C4-vs-0x3E9 trap).
            chosen = raw_s_fit if signed else raw_fit
            chosen_grid = common.sample_hold(g.t, chosen, grid)
            ref_best = sampler(grid, best_lag)
            _, corr_sign = _windowed_spearman_signed(ref_best, chosen_grid, N_WINDOWS)
            plaus = plausibility(chosen[np.isfinite(chosen)], width * 8)["score"]
            r2 = common.linear_fit_r2(chosen_grid, ref_best)[2]
            coverage = float(np.mean(np.isfinite(sampler(grid, 0.0))))
            # high Spearman + low R^2 = monotonic but not linear, a hint the field may be a
            # co-varying proxy (see the divergence check) - a flag, not a conclusion.
            proxy_suspect = bool(best >= PROXY_SPEARMAN and (best - r2) >= PROXY_GAP)
            rec = {
                "id": can_id, "id_hex": f"{can_id:X}", "byte": byte,
                "width": width, "order": order, "signed": signed,
                "score": round(best, 4), "r2": round(r2, 4), "plaus": plaus,
                "corr_sign": corr_sign, "lag_s": round(best_lag, 3),
                "coverage": round(coverage, 2), "proxy_suspect": proxy_suspect,
                "n": g.n,
            }
            # Co-variate discriminator: how well this same field tracks each co-variate (each at its OWN best
            # lag - fair to the co-variate, so the margin never over-claims the target).
            # margin = target Spearman - best co-variate Spearman: strongly + => follows the
            # TARGET; - => follows a co-variate (proxy). Extra cost is one lag-search per
            # co-variate per candidate; correlate stays interactive.
            if cov_samplers:
                cov_best, cov_name = 0.0, None
                for cname, csampler in cov_samplers:
                    cb = max(_windowed_spearman(csampler(grid, lag), chosen_grid, N_WINDOWS)
                             for lag in lags)
                    if cb > cov_best:
                        cov_best, cov_name = cb, cname
                rec["cov_best"] = round(cov_best, 3)
                rec["cov_name"] = cov_name
                rec["margin"] = round(best - cov_best, 3)
            results.append(rec)
    if skipped:
        print(f"(skipped {skipped} candidate field(s) overlapping counter bytes)",
              file=sys.stderr)
    # coarse shortlist: rank by (scale-free) Spearman, then break ties by the
    # scale-AWARE linear-fit R^2, then plausibility. R^2 ahead of plausibility stops a
    # coarse/wrong-scale slice (high Spearman + smooth) from topping genuine carriers
    # that fit the reference linearly - bitsearch remains the field of record.
    results.sort(key=lambda r: (round(r["score"], 2), round(r["r2"], 2),
                                r["plaus"], r["score"]),
                 reverse=True)
    return results          # full sorted list; caller slices [:top] for print/JSON


# ---------------------------------------------------------------------------
# Discrete
# ---------------------------------------------------------------------------

def correlate_discrete(df, sidecar, ids, window_s, top, flagged=None, exclude=None):
    groups = common.group_by_id(df)
    if ids:
        groups = {k: v for k, v in groups.items() if k in ids}
    if exclude:
        groups = {k: v for k, v in groups.items() if k not in exclude}
    events = common.discrete_events(sidecar)
    if len(events) < 2:
        print("ERROR: need >=2 'event' markers in the sidecar.", file=sys.stderr)
        return []

    flagged = flagged or {}
    results = []
    for can_id, g in groups.items():
        bad = flagged.get(can_id, ())
        total_span = g.t[-1] - g.t[0] if g.n > 1 else 1.0
        for bit in range(g.length * 8):
            if bad and (bit // 8) in bad:
                continue  # bit lives in a counter byte
            raw = common.extract_le(g.le_int, bit, 1)
            trans_idx = np.where(np.diff(raw) != 0)[0]
            if len(trans_idx) == 0:
                continue
            trans_t = g.t[trans_idx + 1]
            # events with a bit-transition within +/- window
            hits = 0
            for et in events:
                if np.any(np.abs(trans_t - et) <= window_s):
                    hits += 1
            # baseline expectation under a random transition process
            rate = len(trans_t) / total_span            # transitions / s
            exp = min(1.0, rate * 2 * window_s)          # P(>=1) approx
            lift = (hits / len(events)) - exp            # observed - expected
            if lift <= 0:
                continue
            results.append({
                "id": can_id, "id_hex": f"{can_id:X}", "bit": bit,
                "byte": bit // 8, "bit_in_byte": bit % 8,
                "score": round(float(lift), 4),
                "hits": hits, "events": len(events),
                "transitions": int(len(trans_t)),
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results          # full sorted list; caller slices [:top] for print/JSON


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--sidecar", required=True)
    ap.add_argument("--type", choices=["continuous", "discrete"], required=True)
    ap.add_argument("--ids", help="limit to comma-separated IDs, e.g. 0x123,0x200")
    ap.add_argument("--exclude-ids", help="drop these IDs before searching, e.g. the "
                    "reference source IDs from decode_reference.py (0x7E8) so the search "
                    "can't self-match")
    ap.add_argument("--covariate", action="append", metavar="NAME=SIDECAR",
                    help="continuous: bind a co-variate reference (repeatable) to score "
                         "every candidate against as well, e.g. --covariate rpm=sidecar_rpm.csv "
                         "--covariate map=sidecar_map.csv. Adds a 'margin' column (target "
                         "Spearman - best co-variate Spearman): the deterministic proxy test. "
                         "Strongly positive = the field follows the TARGET; negative = it "
                         "follows a co-variate. Pair with filter_regime.py for the regime split.")
    ap.add_argument("--max-width", type=int, default=2, help="continuous: max bytes")
    ap.add_argument("--max-lag", type=float, default=LAG_WINDOW_S,
                    help=f"continuous: +/- lag search half-window (s) to absorb "
                         f"reference<->trace skew (default {LAG_WINDOW_S:g}). Use ~0.2 for a "
                         f"clean machine reference; the wide default suits a VIDEO reference "
                         f"(creation_time anchor + clock drift). Matches verify.py --max-lag.")
    ap.add_argument("--window", type=float, default=0.3, help="discrete: +/- s match")
    ap.add_argument("--ref-window", type=float,
                    help="holds windows-only: correlate ONLY the fixed window (s) after each "
                         "anchor tag (deliberately-held steady data); transitions excluded")
    ap.add_argument("--ref-guard", type=float, default=0.0,
                    help="seconds to skip at the start of each --ref-window (click jitter)")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--no-skip-flagged", action="store_true",
                    help="don't skip counter/checksum bytes flagged by survey")
    ap.add_argument("--json", help="output JSON (default temp-output/candidates_<label>.json)")
    ap.add_argument("--plots-dir", help="dir for the correlation heatmap PNG "
                    "(default temp-output/analysis-plots/; pass the signal's "
                    "analysis-plots/ folder to keep all step plots together)")
    ap.add_argument("--no-plots", action="store_true", help="skip the heatmap PNG")
    args = ap.parse_args()

    df = common.load_trace(args.trace)
    sidecar = common.load_sidecar(args.sidecar)
    ids = [int(x, 0) for x in args.ids.split(",")] if args.ids else None
    exclude = {int(x, 0) for x in args.exclude_ids.split(",")} if args.exclude_ids else None
    flagged = {} if args.no_skip_flagged else flagged_bytes(args.trace)

    covariates = None
    if args.covariate:
        covariates = []
        for spec in args.covariate:
            if "=" not in spec:
                print(f"--covariate must be NAME=SIDECAR (got {spec!r})", file=sys.stderr)
                return 1
            nm, pth = spec.split("=", 1)
            covariates.append((nm.strip(), common.load_sidecar(pth)))
        if args.type == "discrete":
            print("(--covariate is ignored for --type discrete)", file=sys.stderr)
            covariates = None

    if args.type == "continuous":
        results = correlate_continuous(df, sidecar, ids, args.max_width, args.top,
                                       flagged, exclude,
                                       ref_window=args.ref_window, ref_guard=args.ref_guard,
                                       max_lag=args.max_lag, covariates=covariates)
        cols = ("id_hex", "byte", "width", "order", "signed", "score", "r2", "plaus",
                "lag_s", "coverage")
        if covariates:
            cols += ("cov_name", "margin")
    else:
        results = correlate_discrete(df, sidecar, ids, args.window, args.top,
                                     flagged, exclude)
        cols = ("id_hex", "byte", "bit_in_byte", "score", "hits", "events", "transitions")

    if not results:
        print("No candidates found.", file=sys.stderr)
        return 1

    top = results[:args.top]      # full `results` kept for the heatmap below
    print(f"Top {len(top)} candidates ({args.type}):\n")
    print("  ".join(f"{c:>10}" for c in cols))
    print("-" * (12 * len(cols)))
    for r in top:
        print("  ".join(f"{str(r[c]):>10}" for c in cols))

    stem = Path(args.sidecar).stem.replace("sidecar_", "")
    out = Path(args.json) if args.json else Path(f"temp-output/candidates_{stem}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(top, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")

    if not args.no_plots:
        png = common.resolve_plots_dir(args.plots_dir) / "2-correlation.png"
        common.plot_correlation_heatmap(
            png, results, args.type, winner=top[0],
            title=f"Correlation vs reference ({args.type}, {stem})")
        print(f"Wrote {png}")

    if args.type == "continuous":
        b = top[0]
        # Surface the FULL tie-group (every ID whose score rounds to the winner's), not
        # just the first rival - a large tie-group is the tell of a degenerate ranking
        # the reference can't separate, so the operator doesn't read #1 as confident.
        wsc = round(b["score"], 2)
        tied = [r for r in results if round(r["score"], 2) == wsc]
        tied_ids = sorted({r["id_hex"] for r in tied}, key=lambda h: int(h, 16))
        if len(tied_ids) > 1:
            print(f"\nNote: {len(tied)} candidate field(s) across {len(tied_ids)} ID(s) "
                  f"tie at score {wsc:.2f}: 0x" + ", 0x".join(tied_ids) + ". Ranked "
                  f"within the tie by linear-fit R^2 (column 'r2'): the pick "
                  f"(0x{b['id_hex']}, r2 {b['r2']:.2f}) has the best linear reconstruction "
                  f"of the reference. A large tie-group can be SEVERAL genuine carriers "
                  f"(per-wheel + cluster speed, or a CAN FD frame mirroring a classical one "
                  f"- any is valid) OR a reference too sparse to separate them (stepped "
                  f"holds - use a continuous SWEEP run). CONFIRM the top-R^2 carriers with "
                  f"bitsearch.")
        print(f"\nBest: ID 0x{b['id_hex']} byte {b['byte']} width {b['width']} "
              f"{b['order']}-endian signed={b['signed']} (score {b['score']}, "
              f"lag {b['lag_s']}s)\nNext: build_dbc.py --id 0x{b['id_hex']} "
              f"--byte {b['byte']} --width {b['width']} --order {b['order']} "
              f"--lag {b['lag_s']}" + (" --signed" if b['signed'] else ""))

        # Co-variate discriminator lean for the winner.
        if covariates and b.get("margin") is not None:
            m, cn = b["margin"], b.get("cov_name")
            if m >= COVAR_MARGIN:
                lean = (f"leans TARGET - fits the target better than its closest co-variate "
                        f"{cn} by {m:+.2f} Spearman")
            elif m <= -COVAR_MARGIN:
                lean = (f"leans CO-VARIATE {cn} - this field tracks {cn} MORE than the target "
                        f"({m:+.2f}); a strong proxy signal, not evidence the target is here")
            else:
                lean = (f"TOO CLOSE to call globally ({m:+.2f} vs {cn}) - split them with "
                        f"filter_regime.py and re-run inside the divergence regime")
            print(f"Divergence check: {lean}.")
        # Proxy-suspect hint (soft): high Spearman + low R^2 among the shown candidates.
        proxies = [r for r in top if r.get("proxy_suspect")]
        if proxies:
            tags = ", ".join(f"0x{r['id_hex']}:byte{r['byte']}" for r in proxies)
            print(f"[~] proxy-suspect (high Spearman, low R^2) AFTER ruling out lag / "
                  f"endianness+sign / saturation / reference-semantics: {tags}. Possibly a "
                  f"monotonic proxy - separate it with the divergence regime "
                  f"(filter_regime.py --where …), not a tighter fit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
