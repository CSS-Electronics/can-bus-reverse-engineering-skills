"""Parse Car Scanner OBD-II CSV exports and resample them onto a uniform time grid.

See CLAUDE.md for the file format and resampling rules this module implements.
"""

import numpy as np
import pandas as pd

# Exact PID strings -> (output column name, resample method).
# method is "linear" (smooth continuous signals) or "hold" (step / discrete signals).
# This dict is just a `signals` argument for to_wide() — it doesn't affect what
# parse_log() stores. Every PID (including the throttle ones below) stays in the
# raw long table regardless of what's included here.
CORE_SIGNALS = {
    "Vehicle speed": {"column": "vehicle_speed_mph", "method": "linear"},
    "Engine RPM": {"column": "engine_rpm", "method": "linear"},
    "Vehicle acceleration": {"column": "vehicle_accel_g", "method": "linear"},
}

# Throttle candidates, left out of CORE_SIGNALS this session: "Absolute pedal
# position E" logged only once (a single coincidental value), and "Throttle
# position" / "Relative throttle position" are too sparse to trust. Revisit
# once throttle is available via CAN. To bring one back:
#   CORE_SIGNALS["Absolute pedal position E"] = THROTTLE_SIGNALS["Absolute pedal position E"]
THROTTLE_SIGNALS = {
    "Absolute pedal position E": {"column": "throttle_pct_pedal_e", "method": "hold"},
    "Relative throttle position": {"column": "throttle_pct_relative", "method": "hold"},
    "Throttle position": {"column": "throttle_pct", "method": "hold"},
}


def parse_log(path):
    """Read a Car Scanner long-format CSV export into a tidy long DataFrame.

    Handles the export's quirks: ';' delimiter, quoted fields, a trailing
    empty column from the trailing ';' on every row, and the misspelled
    LONGTITUDE header.

    Returns a DataFrame with columns: time, pid, value, units, lat, lon,
    sorted by time. Values are left as raw readings (no resampling).
    """
    df = pd.read_csv(path, sep=";", quotechar='"', engine="c")
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    df = df.rename(
        columns={
            "SECONDS": "time",
            "PID": "pid",
            "VALUE": "value",
            "UNITS": "units",
            "LATITUDE": "lat",
            "LONGTITUDE": "lon",
        }
    )
    df["time"] = df["time"].astype(float)
    df["value"] = df["value"].astype(float)
    return df.sort_values("time").reset_index(drop=True)


def _gap_mask(grid, x, threshold):
    """True for grid points that fall inside a raw-sample gap wider than threshold.

    A "gap" is any stretch with no raw sample: between two consecutive raw
    samples spaced more than `threshold` apart, or before the first / after
    the last raw sample if that edge distance exceeds `threshold`. Based
    purely on sample timing, independent of the values themselves.
    """
    mask = np.zeros(len(grid), dtype=bool)

    if x[0] - grid[0] > threshold:
        mask |= grid < x[0]
    if grid[-1] - x[-1] > threshold:
        mask |= grid > x[-1]

    gap_starts = np.where(np.diff(x) > threshold)[0]
    for i in gap_starts:
        lo, hi = x[i], x[i + 1]
        mask |= (grid > lo) & (grid < hi)

    return mask


def to_wide(df, signals, hz=10, gap_threshold=5.0):
    """Pivot selected signals onto a uniform time grid.

    df: long DataFrame from parse_log (columns: time, pid, value, ...).
    signals: dict mapping exact PID string -> {"column": out_name, "method": "linear"|"hold"}.
    hz: grid rate in Hz (default 10 -> one row every 0.1s).
    gap_threshold: raw-sample gaps wider than this (seconds) mark grid points as
        gapped. Interpolated/held values are still filled in as usual — the gap
        flag only marks that they're bridging a stretch with no samples.

    Every signal's own timestamps are used to fill every grid point (hold-last
    or linear interpolation, per signal) — no two PIDs share a timestamp in
    the raw log, so each is resampled independently onto the shared grid.
    Each signal also gets its own `is_gap_<column>` flag column, computed from
    that signal's own raw sample timing — a signal sampled much slower than the
    others (e.g. throttle vs. speed/RPM) gets its own gap regions, not the
    union of everyone else's.

    Raises ValueError if a requested PID has no readings in df.
    """
    t0 = df["time"].min()
    t1 = df["time"].max()
    n_steps = int(round((t1 - t0) * hz))
    grid = t0 + np.arange(n_steps + 1) / hz

    out = pd.DataFrame({"time": grid})
    for pid, spec in signals.items():
        sub = df.loc[df["pid"] == pid, ["time", "value"]].sort_values("time")
        if sub.empty:
            raise ValueError(f"No readings found for PID {pid!r}")

        x = sub["time"].to_numpy()
        y = sub["value"].to_numpy()
        method = spec.get("method", "linear")
        col = spec.get("column", pid)

        if method == "linear":
            out[col] = np.interp(grid, x, y)
        elif method == "hold":
            idx = np.clip(np.searchsorted(x, grid, side="right") - 1, 0, len(x) - 1)
            out[col] = y[idx]
        else:
            raise ValueError(f"Unknown method {method!r} for signal {pid!r}")

        out[f"is_gap_{col}"] = _gap_mask(grid, x, gap_threshold)

    return out
