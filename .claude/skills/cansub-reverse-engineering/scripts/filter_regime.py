"""
filter_regime.py - slice a raw trace + a target sidecar to a physical OPERATING REGIME
defined from reference (co-variate) channels.

The deterministic half of the "divergence-regime" proxy test (see SKILL.md).
Collinear signals (torque vs airflow/RPM/pedal) cannot
be separated by a global correlation because they move together everywhere - EXCEPT in
the operating window where they physically diverge (engine overrun: torque goes negative
while airflow stays low-positive). This script isolates that window: give it the raw
trace, the target sidecar, and the co-variate channels as their own sidecars, plus a
boolean `--where`; it keeps only the trace frames and target samples whose time falls
inside the regime, so `correlate`/`bitsearch` re-run on the filtered files reveal whether
a candidate field tracks the TARGET or just a co-variate.

It reports retained %, intervals, and per-ref inside-vs-outside ranges; it does not judge
the result. A rare regime is flagged as noisy, not rejected.

Example (is the 0x200 field torque, or the airflow it rides on?):
    # build target + co-variate sidecars on the same time base first, then:
    python filter_regime.py --trace temp-output/trace_mustang.csv \
        --sidecar temp-output/sidecar_torque.csv \
        --ref rpm=temp-output/sidecar_rpm.csv --ref pedal=temp-output/sidecar_pedal.csv \
        --where "rpm > 1800 and pedal < 2"
    # -> filtered trace + sidecar under temp-output/, then re-run correlate on them.
"""
from __future__ import annotations

import argparse
import ast
import operator as op
import sys
from pathlib import Path

import numpy as np

import common  # noqa: E402

# --- safe --where evaluator -------------------------------------------------------
# Only these AST nodes are allowed, so a --where string can compare/combine the bound
# ref arrays and numbers but cannot call functions, index, access attributes, or reach
# any builtin - i.e. it can't run arbitrary code.
_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
            ast.USub, ast.UAdd, ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq,
            ast.NotEq, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Name,
            ast.Load, ast.Constant)
_CMP = {ast.Lt: op.lt, ast.LtE: op.le, ast.Gt: op.gt, ast.GtE: op.ge,
        ast.Eq: op.eq, ast.NotEq: op.ne}
_BIN = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv}
_UNARY = {ast.USub: op.neg, ast.UAdd: op.pos}


def _eval_where(expr: str, env: dict[str, np.ndarray]) -> np.ndarray:
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            sys.exit(f"--where: disallowed expression element {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in env:
            sys.exit(f"--where: unknown name {node.id!r} - bind it with "
                     f"--ref {node.id}=<sidecar>")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.BoolOp):
            reduce = np.logical_and if isinstance(n.op, ast.And) else np.logical_or
            out = ev(n.values[0])
            for v in n.values[1:]:
                out = reduce(out, ev(v))
            return out
        if isinstance(n, ast.UnaryOp):
            return (np.logical_not(ev(n.operand)) if isinstance(n.op, ast.Not)
                    else _UNARY[type(n.op)](ev(n.operand)))
        if isinstance(n, ast.BinOp):
            return _BIN[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.Compare):
            left, out = ev(n.left), None
            for o, comp in zip(n.ops, n.comparators):
                right = ev(comp)
                r = _CMP[type(o)](left, right)
                out = r if out is None else np.logical_and(out, r)
                left = right
            return out
        if isinstance(n, ast.Name):
            return env[n.id]
        if isinstance(n, ast.Constant):
            return n.value
        sys.exit(f"--where: unsupported node {type(n).__name__}")

    return np.asarray(ev(tree), dtype=bool)


def _load_ref(spec: str):
    """Parse a --ref name=path binding into (name, times, values)."""
    if "=" not in spec:
        sys.exit(f"--ref must be name=path (got {spec!r})")
    name, path = spec.split("=", 1)
    t, v = common.continuous_reference(common.load_sidecar(path))
    if t.size == 0:
        sys.exit(f"--ref {name}: {path} has no kind=value rows")
    return name.strip(), t, v


def _epochs(path: str) -> tuple[str, list[str], np.ndarray]:
    """Read a semicolon CSV (webCAN trace or sidecar); return header, body lines, and
    the first-column epoch of each row (both formats put the epoch first)."""
    lines = Path(path).read_text().splitlines()
    if not lines:
        sys.exit(f"{path} is empty")
    body = [ln for ln in lines[1:] if ln.strip()]
    ep = np.array([float(ln.split(";", 1)[0]) for ln in body], dtype=np.float64)
    return lines[0], body, ep


def _in_intervals(ep: np.ndarray, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    if starts.size == 0:
        return np.zeros(ep.shape, dtype=bool)
    idx = np.searchsorted(starts, ep, side="right") - 1
    ok = idx >= 0
    idxc = np.clip(idx, 0, len(ends) - 1)
    return ok & (ep <= ends[idxc])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True, help="raw webCAN CSV to filter")
    ap.add_argument("--sidecar", help="target sidecar to filter to the same regime")
    ap.add_argument("--ref", action="append", required=True, metavar="NAME=SIDECAR",
                    help="bind a co-variate channel usable in --where (repeatable), "
                         "e.g. --ref rpm=temp-output/sidecar_rpm.csv")
    ap.add_argument("--where", required=True,
                    help="boolean regime over the --ref names, e.g. "
                         "\"rpm > 1800 and pedal < 2\"")
    ap.add_argument("--min-dwell", type=float, default=0.0,
                    help="drop regime intervals shorter than this many seconds "
                         "(debounce threshold flicker; default 0 = keep all)")
    ap.add_argument("--out-trace", help="filtered trace (default temp-output/"
                                        "<trace-stem>_regime.csv)")
    ap.add_argument("--out-sidecar", help="filtered sidecar (default temp-output/"
                                          "<sidecar-stem>_regime.csv)")
    args = ap.parse_args(argv)

    refs = [_load_ref(s) for s in args.ref]

    # Evaluate the regime on the union of all ref sample times (each ref interpolated
    # onto that grid; out-of-range -> NaN, which fails every comparison and so is
    # excluded). Contiguous True runs become the regime intervals.
    grid = np.unique(np.concatenate([t for _, t, _ in refs]))
    env = {name: np.interp(grid, t, v, left=np.nan, right=np.nan)
           for name, t, v in refs}
    mask = _eval_where(args.where, env)
    if mask.shape != grid.shape:              # a constant --where (no ref) -> broadcast
        mask = np.broadcast_to(mask, grid.shape)

    edges = np.diff(mask.astype(np.int8))
    starts = grid[np.flatnonzero(edges == 1) + 1]
    ends = grid[np.flatnonzero(edges == -1)]
    if mask[0]:
        starts = np.insert(starts, 0, grid[0])
    if mask[-1]:
        ends = np.append(ends, grid[-1])
    if args.min_dwell > 0 and starts.size:     # debounce short intervals
        keep = (ends - starts) >= args.min_dwell
        starts, ends = starts[keep], ends[keep]

    span = float(grid[-1] - grid[0]) or 1.0
    dwell = float(np.sum(ends - starts))
    print(f'regime "{args.where}":  {starts.size} interval(s)'
          + (f' (min-dwell >= {args.min_dwell:g}s)' if args.min_dwell > 0 else '')
          + f', {dwell:.1f}s of {span:.1f}s ({100 * dwell / span:.1f}% of the span)')

    # Filter each file by interval membership of its own epoch column.
    out_trace = args.out_trace or f"temp-output/{Path(args.trace).stem}_regime.csv"
    Path(out_trace).parent.mkdir(parents=True, exist_ok=True)
    header, body, ep = _epochs(args.trace)
    keep = _in_intervals(ep, starts, ends)
    Path(out_trace).write_text("\n".join([header] + [body[i] for i in np.flatnonzero(keep)])
                               + "\n", newline="")
    print(f"  trace:   kept {int(keep.sum())} / {len(body)} frames "
          f"({100 * keep.mean():.1f}%) -> {out_trace}")

    kept_sc = None
    if args.sidecar:
        out_sc = args.out_sidecar or f"temp-output/{Path(args.sidecar).stem}_regime.csv"
        h2, b2, ep2 = _epochs(args.sidecar)
        k2 = _in_intervals(ep2, starts, ends)
        kept_sc = int(k2.sum())
        Path(out_sc).write_text("\n".join([h2] + [b2[i] for i in np.flatnonzero(k2)])
                                + "\n", newline="")
        print(f"  sidecar: kept {kept_sc} / {len(b2)} samples "
              f"({100 * k2.mean():.1f}%) -> {out_sc}")

    # Sanity: each ref's range inside vs outside the regime - confirms the mask really
    # isolated the intended window (e.g. pedal ~0 inside, wide outside).
    print("  ref ranges inside vs outside the regime:")
    for name, t, v in refs:
        g = env[name]
        fin = np.isfinite(g)
        ins, out = g[fin & mask], g[fin & ~mask]
        istr = f"{ins.min():g}..{ins.max():g}" if ins.size else "(none)"
        ostr = f"{out.min():g}..{out.max():g}" if out.size else "(none)"
        print(f"    {name:10s} inside {istr:>18s}   outside {ostr}")

    # Guardrail: a rare regime yields noisy, low-N results.
    retained = kept_sc if kept_sc is not None else int(keep.sum())
    if 100 * keep.mean() < 1.0 or retained < 200:
        print("  [!] very little data retained - this regime is rare in THIS log; a "
              "field's behaviour inside it will be noisy. Consider a log/segment that "
              "exercises the regime more, or a looser --where.")
    print("  next: re-run correlate/bitsearch on the filtered files, and compare a "
          "candidate's fit to the TARGET vs each co-variate inside the regime.")


if __name__ == "__main__":
    main()
