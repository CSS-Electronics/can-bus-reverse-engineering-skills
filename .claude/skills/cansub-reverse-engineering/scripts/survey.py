"""
survey.py - statistical overview + bit-activity heatmap for a CAN trace.

For each CAN ID: frame count, mean cycle time + jitter, payload length, latest
payload, and per-byte / per-bit activity (which bits/bytes are static vs
changing, byte entropy). Each byte is classified static | counter | checksum |
signal: rolling counters [ctr@] are excluded from later correlation; checksum-like
bytes [cks@] are flagged as a hint only. Works for classical CAN and CAN FD. This
is the deterministic replacement for eyeballing webCAN fade-mode: use it to shrink
the candidate-ID set before correlating.

Bit indices (changing_bits, flag_bits, field_map) are LSB-FIRST global indices
(k = byte*8 + bit_in_byte, bit 0 = byte LSB) - the same convention bitsearch /
build_dbc use, so a survey bit index drops straight into them. For a densely
bit-packed frame it also proposes a heuristic FIELD MAP (where sub-byte fields
likely start) and flags a constant leading bit (e.g. a 'valid' flag) that pushes
fields off byte boundaries - both hints to seed bitsearch.

Examples:
    python survey.py --trace temp-output/trace_baseline.csv
    python survey.py --trace temp-output/trace_baseline.csv --json temp-output/survey.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

import common


def _bit_transition_rates(byte_arr: np.ndarray, modal: int):
    """Per-bit flip-rate + changing mask in LSB-FIRST global index.

    Thin wrapper over `common.bit_flip_rates` (the single source of truth, shared with
    the cascade resolution refinement) so survey and bitsearch use the identical
    convention: global bit k = byte*8 + bit_in_byte, bit_in_byte 0 = the byte's LSB.
    """
    return common.bit_flip_rates(byte_arr, modal)


def _propose_fields(rate: np.ndarray, changing: np.ndarray, nbits: int,
                    rise: float = 1.8):
    """Heuristic little-endian field map (a HINT, not a claim).

    Within a packed region a field's bits run LSB (high flip-rate) -> MSB (low
    flip-rate); a field BOUNDARY is where the flip-rate JUMPS back up - the next
    field's LSB toggling. Constant bits at/inside the region's low end (e.g. a
    valid flag at bit 0) are reported separately as flag_bits, since they push the
    real fields off byte boundaries. Returns (flag_bits, [(start_bit, length)...]).
    """
    active = [k for k in range(nbits) if changing[k]]
    if len(active) < 4:
        return [], []
    lo, hi = active[0], active[-1]
    flag_bits = [k for k in range(0, hi + 1) if not changing[k]]
    starts = [lo]
    prev = lo
    for k in range(lo + 1, hi + 1):
        if not changing[k]:
            continue
        if rate[k] > rise * max(rate[prev], 1e-6):
            starts.append(k)
        prev = k
    fields = []
    for i, s in enumerate(starts):
        end = (starts[i + 1] - 1) if i + 1 < len(starts) else hi
        fields.append((s, end - s + 1))
    return flag_bits, fields


def _entropy(values: np.ndarray) -> float:
    _, counts = np.unique(values, return_counts=True)
    p = counts / counts.sum()
    return float(-(p * np.log2(p)).sum())


def _counter_fraction(vals: np.ndarray, mod: int) -> float:
    """Fraction of consecutive frames where the value increments by +1 (mod `mod`)."""
    if len(vals) < 3:
        return 0.0
    d = np.diff(vals.astype(np.int64)) % mod
    return float(np.mean(d == 1))


def _classify_byte(col: np.ndarray, entropy: float) -> str:
    """Classify one time-ordered byte column: static | counter | checksum | signal.

    counter  - the byte, or either nibble, increments by +1 almost every frame
               (rolling message counter; not a real signal).
    checksum - near-unique values every frame + high entropy (CRC/checksum-like;
               uncorrelated, should not be chased as a signal).
    """
    if col.size == 0:
        return "static"
    distinct = len(np.unique(col))
    if distinct <= 1:
        return "static"
    fr = max(_counter_fraction(col, 256),
             _counter_fraction(col & 0x0F, 16),
             _counter_fraction(col >> 4, 16))
    if fr >= 0.9:
        return "counter"
    # checksum/CRC-like: near-uniform over the byte (high entropy) AND changes
    # almost every frame. (A real signal byte changes far less or isn't uniform.)
    change_rate = float(np.mean(np.diff(col.astype(np.int64)) != 0)) if col.size > 1 else 0.0
    if entropy >= 7.5 and change_rate >= 0.9:
        return "checksum"
    return "signal"


def _repeat_unit(vals: list[int]):
    """If a fully-static payload is a repeating short unit (period 1/2/4 bytes),
    return that unit as an upper-hex string, else None. Catches the seductive
    "N identical mid-scale bytes" pattern (8x0x80, 0080x4, ...) that is almost
    always zeroed/offset-encoded counters or unused channels, not N signals."""
    n = len(vals)
    if n == 0:
        return None
    for p in (1, 2, 4):
        if p < n and n % p == 0 and all(vals[i] == vals[i % p] for i in range(n)):
            return bytes(vals[:p]).hex().upper()
    return None


def survey(df):
    rows = []
    rates_by_id = {}     # can_id -> per-bit flip-rate array (for the heatmap)
    for can_id, g in df.groupby("id"):
        g = g.sort_values("t")
        t = g["t"].to_numpy(np.float64)
        data = list(g["data"])
        lengths = g["length"].to_numpy()
        modal = int(np.bincount(lengths).argmax())
        ext = bool(g["ext"].iloc[0])

        dt = np.diff(t)
        period_ms = float(np.mean(dt) * 1000) if len(dt) else 0.0
        jitter_ms = float(np.std(dt) * 1000) if len(dt) else 0.0

        same = [d for d in data if len(d) == modal]
        byte_arr = np.array([list(d[:modal]) for d in same], dtype=np.uint8) \
            if same else np.zeros((0, modal), np.uint8)
        rate, changing = _bit_transition_rates(byte_arr, modal)
        rates_by_id[int(can_id)] = rate
        changing_bits = [k for k in range(modal * 8) if changing[k]]  # LSB-first
        flag_bits, field_map = _propose_fields(rate, changing, modal * 8)

        byte_info = []
        for b in range(modal):
            col = byte_arr[:, b] if byte_arr.size else np.array([], np.uint8)
            distinct = int(len(np.unique(col))) if col.size else 0
            ent = round(_entropy(col), 3) if col.size else 0.0
            byte_info.append({
                "byte": b,
                "distinct": distinct,
                "min": int(col.min()) if col.size else 0,
                "max": int(col.max()) if col.size else 0,
                "entropy": ent,
                "changing": distinct > 1,
                "class": _classify_byte(col, ent),
            })

        changing_bytes = [bi["byte"] for bi in byte_info if bi["changing"]]
        const_vals = [bi["min"] for bi in byte_info]  # static byte -> min==max==value
        all_static = bool(byte_arr.size) and not changing_bytes
        const_unit = _repeat_unit(const_vals) if all_static else None

        rows.append({
            "id": int(can_id),
            "id_hex": f"{can_id:X}",
            "ext": ext,
            "count": int(len(g)),
            "length": modal,
            "period_ms": round(period_ms, 2),
            "jitter_ms": round(jitter_ms, 2),
            "latest_payload": same[-1].hex().upper() if same else "",
            "changing_bytes": changing_bytes,
            "all_static": all_static,
            "const_unit": const_unit,
            "counter_bytes": [bi["byte"] for bi in byte_info if bi["class"] == "counter"],
            "checksum_bytes": [bi["byte"] for bi in byte_info if bi["class"] == "checksum"],
            "changing_bits": changing_bits,        # LSB-first global index
            "flag_bits": flag_bits,                # constant bits offsetting fields
            "field_map": [{"start_bit": s, "length": l} for s, l in field_map],
            "bytes": byte_info,
        })
    rows.sort(key=lambda r: r["id"])
    return rows, rates_by_id


def print_report(rows):
    print(f"{'ID':>8} {'ext':>3} {'N':>7} {'len':>3} {'period':>8} {'jit':>6} "
          f"{'changing bytes':<20} latest")
    print("-" * 90)
    for r in rows:
        cb = ",".join(str(b) for b in r["changing_bytes"]) or "-"
        fl = []
        if r["counter_bytes"]:
            fl.append("ctr@" + ",".join(str(b) for b in r["counter_bytes"]))
        if r["checksum_bytes"]:
            fl.append("cks@" + ",".join(str(b) for b in r["checksum_bytes"]))
        flags = ("  " + " ".join(fl)) if fl else ""
        print(f"{r['id_hex']:>8} {('Y' if r['ext'] else 'N'):>3} {r['count']:>7} "
              f"{r['length']:>3} {r['period_ms']:>7.1f}m {r['jitter_ms']:>5.1f} "
              f"{cb:<20} {r['latest_payload']}{flags}")
    n_changing = sum(1 for r in rows if r["changing_bytes"])
    n_ctr = sum(len(r["counter_bytes"]) for r in rows)
    n_cks = sum(len(r["checksum_bytes"]) for r in rows)
    print("-" * 90)
    print(f"{len(rows)} IDs, {n_changing} with changing bytes "
          f"(candidate set for a varying signal).")
    if n_ctr or n_cks:
        print(f"Flagged {n_ctr} counter byte(s) [ctr@] (correlate skips these) and "
              f"{n_cks} checksum-like byte(s) [cks@] (heuristic hint only - a wide "
              f"signal's low byte can look like one, so these are NOT skipped).")

    # Static frames on a calm scan are EXPECTED and conclude NOTHING (the parked target
    # looks static too). The useful signal is the baseline-vs-exercised DELTA, not a
    # hypothesis - positive OR negative - read off this single scan. We just list them so
    # the delta has a reference, and flag tidy constants that tend to invite a false story.
    static = [r for r in rows if r.get("all_static")]
    if static:
        print(f"\n[note] {len(static)} of {len(rows)} ID(s) have NO changing bytes in this "
              f"capture. On a calm/baseline scan that is EXPECTED (a parked target looks static "
              f"too), so a byte pattern here is NOT evidence of a signal OR a non-signal - don't "
              f"form a hypothesis from this scan. What tells you something is the DELTA vs an "
              f"exercised/sweep scan (re-run with --baseline) plus correlation:")
        for r in static:
            if r.get("const_unit"):
                n_sig = r["length"] // max(1, len(r["const_unit"]) // 2)
                print(f"  0x{r['id_hex']}: {r['latest_payload']}  (tidy constant "
                      f"{r['const_unit']}x{n_sig} - invites a false 'N signals' story; resist it, "
                      f"it is just as likely zeroed counters / unused channels)")
            else:
                print(f"  0x{r['id_hex']}: {r['latest_payload']}  (static)")

    # Proposed bit-packed field maps (hint): LSB-first start bits feed bitsearch.
    mapped = [r for r in rows if r.get("field_map")]
    if mapped:
        print("\nProposed field maps (HINT - little-endian, LSB-first start bits; "
              "verify with bitsearch):")
        for r in mapped:
            fm = " ".join(f"{f['start_bit']}+{f['length']}" for f in r["field_map"])
            flags = (f"  flag/static bits: {r['flag_bits']}" if r.get("flag_bits") else "")
            print(f"  0x{r['id_hex']}: {fm}{flags}")
        print("  (start+length in bits; a leading flag bit, e.g. a 'valid' bit at 0, "
              "shifts every field off byte boundaries - run bitsearch per field.)")

    # Authoritative ID total LAST so it survives a `tail` of this (long) report. The
    # field-maps list above is only the bit-packed SUBSET - don't read the ID count
    # off it. The survey JSON is the full machine-readable ID set.
    print(f"\n== {len(rows)} unique IDs surveyed (full list in the survey JSON) ==")


def print_active_delta(rows, base_rows) -> None:
    """Diff changing-bytes vs a prior (baseline) survey JSON: what became active."""
    base = {(r["id_hex"], b) for r in base_rows for b in r.get("changing_bytes", [])}
    base_ids = {r["id_hex"] for r in base_rows}
    new_by_id = {}
    for r in rows:
        new = [b for b in r["changing_bytes"] if (r["id_hex"], b) not in base]
        if new:
            new_by_id[r["id_hex"]] = (new, r["id_hex"] not in base_ids)
    print("\nNewly-active bytes vs baseline (the exercised target should appear here):")
    if not new_by_id:
        print("  (nothing newly active - did the target actually move during this capture?)")
        return
    for idh, (active, new_id) in sorted(new_by_id.items()):
        tag = " [new ID]" if new_id else ""
        print(f"  0x{idh}: bytes {','.join(map(str, active))}{tag}")
    print("  Counters/pulse inputs can also activate, so this NARROWS the field - "
          "correlate against the sweep reference + bitsearch decide which it is.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True)
    ap.add_argument("--baseline", help="a prior survey JSON (e.g. the steady baseline) to "
                    "diff against: prints which IDs/bytes became active vs that scan so the "
                    "exercised target stands out (counters/pulse frames may also activate)")
    ap.add_argument("--exclude-ids", help="drop these IDs before surveying, e.g. the "
                    "reference source IDs from decode_reference.py (0x7E8)")
    ap.add_argument("--json", help="write machine-readable survey JSON here")
    ap.add_argument("--plots-dir", help="dir for the bus bit-activity heatmap PNG "
                    "(default temp-output/analysis-plots/; pass the signal's "
                    "analysis-plots/ folder to keep all step plots together)")
    ap.add_argument("--no-plots", action="store_true", help="skip the heatmap PNG")
    ap.add_argument("--suffix", default="", help="suffix for the heatmap filename so "
                    "two surveys don't overwrite, e.g. --suffix steady -> "
                    "1-survey-bus-activity-steady.png (and --suffix variable for the "
                    "trace recorded while exercising the target signal)")
    args = ap.parse_args()

    df = common.load_trace(args.trace)
    if args.exclude_ids:
        excl = {int(x, 0) for x in args.exclude_ids.split(",")}
        df = df[~df["id"].isin(excl)]
    if df.empty:
        print("No RX frames in trace.", file=sys.stderr)
        return 1
    rows, rates_by_id = survey(df)
    print_report(rows)

    if args.baseline:
        try:
            base_rows = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[!] --baseline not read ({exc})", file=sys.stderr)
        else:
            print_active_delta(rows, base_rows)

    out = Path(args.json) if args.json else common.default_survey_json(args.trace)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")

    if not args.no_plots:
        label = Path(args.trace).stem.replace("trace_", "")
        sfx = f"-{args.suffix}" if args.suffix else ""
        title = f"CAN bus bit-activity ({label}{', ' + args.suffix if args.suffix else ''})"
        png = common.resolve_plots_dir(args.plots_dir) / f"1-survey-bus-activity{sfx}.png"
        common.plot_bus_activity(png, rows, rates_by_id, title=title)
        print(f"Wrote {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
