"""Sanity-check the parser/resampler against a real Car Scanner export.

Usage: python validate.py <path-to-csv> [output-dir-for-plot]
"""

import sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

from parser import CORE_SIGNALS, parse_log, to_wide


def _gap_spans(time, is_gap):
    """Contiguous (start, end) time spans where is_gap is True."""
    edges = np.flatnonzero(np.diff(np.concatenate(([False], is_gap, [False]))))
    return list(zip(time.to_numpy()[edges[0::2]], time.to_numpy()[edges[1::2] - 1]))


def main():
    csv_path = sys.argv[1]
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".")

    long_df = parse_log(csv_path)
    wide_df = to_wide(long_df, CORE_SIGNALS, hz=10, gap_threshold=5.0)

    print(f"long table: {long_df.shape}")
    print(f"wide table: {wide_df.shape}")
    print(wide_df.head(10).to_string())
    print()
    for spec in CORE_SIGNALS.values():
        gap_col = f"is_gap_{spec['column']}"
        n_gap = wide_df[gap_col].sum()
        print(f"{gap_col}: {n_gap} / {len(wide_df)} grid points ({100 * n_gap / len(wide_df):.1f}%)")
    print()

    # Per-signal sanity checks on the RAW readings (before resampling).
    print("raw signal sanity:")
    for pid in CORE_SIGNALS:
        sub = long_df.loc[long_df["pid"] == pid].sort_values("time")
        n = len(sub)
        span = long_df["time"].max() - long_df["time"].min()
        rate = n / span if span else float("nan")
        gaps = sub["time"].diff().dropna()
        max_gap = gaps.max() if not gaps.empty else float("nan")
        print(
            f"  {pid!r}: n={n}, rate~{rate:.2f} Hz, "
            f"value range=[{sub['value'].min():.3g}, {sub['value'].max():.3g}], "
            f"max gap between raw samples={max_gap:.2f}s"
        )

    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)

    def shade_gaps(ax, gap_col):
        for s, e in _gap_spans(wide_df["time"], wide_df[gap_col].to_numpy()):
            ax.axvspan(s, e, color="red", alpha=0.15, linewidth=0)

    axes[0].plot(wide_df["time"], wide_df["vehicle_speed_mph"], linewidth=0.8)
    axes[0].set_ylabel("Speed (mph)")
    axes[0].axhline(0, color="gray", linewidth=0.5, linestyle="--")
    shade_gaps(axes[0], "is_gap_vehicle_speed_mph")

    axes[1].plot(wide_df["time"], wide_df["engine_rpm"], linewidth=0.8, color="tab:orange")
    axes[1].set_ylabel("Engine RPM")
    axes[1].set_xlabel("Time (s, session clock)")
    shade_gaps(axes[1], "is_gap_engine_rpm")

    fig.suptitle("10 Hz resampled wide table — speed / RPM over full drive (red = own is_gap)")
    fig.tight_layout()

    out_path = out_dir / "validation_plot.png"
    fig.savefig(out_path, dpi=130)
    print(f"\nSaved plot to {out_path}")


if __name__ == "__main__":
    main()
