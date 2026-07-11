#!/usr/bin/env python3
"""
Plot prefetch interaction percentages from a sweep summary.csv.

Two plot families:
  1) vs_window/   — for each (trace, cache_size):
                    y = interaction %  vs  x = interaction window
  2) vs_cache_size/ — for each (trace, interaction_window):
                    y = interaction %  vs  x = cache size

Each figure has two panels:
  - prefetch→evict / prefetches
  - evict→prefetch / evictions

Lines are one per (eviction, prefetcher) combo.

Example:
  ./scripts/plot_prefetch_interactions.py \\
      --summary ./results/cp_sweep/summary.csv \\
      --outdir ./results/cp_sweep/plots
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

CACHE_SIZE_ORDER = ["16mb", "64mb", "256mb", "1gb", "4mb", "8mb", "32mb", "128mb", "512mb", "2gb"]


def parse_size_bytes(size: str) -> int:
    s = str(size).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(kb|mb|gb|tb|b)?", s)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2) or "b"
    mult = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}[unit]
    return int(val * mult)


def load_summary(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df[df["trace"].astype(str).str.endswith((".zst", ".bin", ".vscsi", ".csv", ".txt", ".lcs"))].copy()
    # drop accidental non-trace artifacts
    df = df[~df["trace"].astype(str).str.startswith("request_counts")].copy()

    for c in [
        "interaction_window",
        "n_prefetch",
        "n_prefetch_then_evict",
        "n_evict",
        "n_evict_then_prefetch",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df[df["status"].astype(str).str.startswith("ok") | df["status"].isna()].copy()
    # window 0 disables tracking
    df = df[df["interaction_window"] > 0].copy()

    df["pct_pf_evict"] = (
        100.0 * df["n_prefetch_then_evict"] / df["n_prefetch"].where(df["n_prefetch"] > 0)
    )
    df["pct_ev_pf"] = (
        100.0 * df["n_evict_then_prefetch"] / df["n_evict"].where(df["n_evict"] > 0)
    )
    df["combo"] = df["eviction"].astype(str) + "+" + df["prefetcher"].astype(str)
    df["cache_size"] = df["cache_size"].astype(str)
    df["trace_stem"] = df["trace"].astype(str).map(lambda t: Path(t).name)
    return df


def _style_for_combos(combos: list[str]):
    cmap = plt.get_cmap("tab20")
    styles = ["-", "--", "-.", ":"]
    out = {}
    for i, c in enumerate(sorted(combos)):
        out[c] = (cmap(i % 20), styles[i % len(styles)])
    return out


def _save(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def plot_vs_window(df: pd.DataFrame, outdir: Path) -> int:
    """One plot per (trace, cache_size): % vs interaction window."""
    n = 0
    for (trace, cache_size), g in df.groupby(["trace_stem", "cache_size"], sort=True):
        combos = g["combo"].unique().tolist()
        styles = _style_for_combos(combos)

        fig, axes = plt.subplots(1, 2, figsize=(11, 4), sharex=True)
        for ax, ycol, title in [
            (axes[0], "pct_pf_evict", "prefetch→evict / prefetches"),
            (axes[1], "pct_ev_pf", "evict→prefetch / evictions"),
        ]:
            for combo, cg in g.groupby("combo"):
                cg = cg.sort_values("interaction_window")
                color, ls = styles[combo]
                ax.plot(
                    cg["interaction_window"],
                    cg[ycol],
                    marker="o",
                    linewidth=1.5,
                    markersize=4,
                    color=color,
                    linestyle=ls,
                    label=combo,
                )
            ax.set_xscale("log")
            ax.set_xlabel("interaction window (requests)")
            ax.set_ylabel("percentage (%)")
            ax.set_title(title)
            ax.set_ylim(bottom=0)
            ax.grid(True, which="both", alpha=0.3)

        handles, labels = axes[0].get_legend_handles_labels()
        if handles:
            fig.legend(
                handles,
                labels,
                loc="upper center",
                ncol=min(4, len(handles)),
                fontsize=8,
                frameon=False,
                bbox_to_anchor=(0.5, 1.12),
            )
        fig.suptitle(f"{trace}  |  cache={cache_size}", y=1.16, fontsize=12)

        out = outdir / "vs_window" / trace / f"cache_{cache_size}.png"
        _save(fig, out)
        n += 1
    return n


def plot_vs_cache_size(df: pd.DataFrame, outdir: Path) -> int:
    """One plot per (trace, interaction_window): % vs cache size."""
    n = 0
    # stable size order
    present_sizes = sorted(df["cache_size"].unique(), key=parse_size_bytes)
    size_to_x = {s: i for i, s in enumerate(present_sizes)}

    for (trace, window), g in df.groupby(["trace_stem", "interaction_window"], sort=True):
        combos = g["combo"].unique().tolist()
        styles = _style_for_combos(combos)

        fig, axes = plt.subplots(1, 2, figsize=(11, 4), sharex=True)
        for ax, ycol, title in [
            (axes[0], "pct_pf_evict", "prefetch→evict / prefetches"),
            (axes[1], "pct_ev_pf", "evict→prefetch / evictions"),
        ]:
            for combo, cg in g.groupby("combo"):
                cg = cg.copy()
                cg["x"] = cg["cache_size"].map(size_to_x)
                cg = cg.dropna(subset=["x"]).sort_values("x")
                color, ls = styles[combo]
                ax.plot(
                    cg["x"],
                    cg[ycol],
                    marker="o",
                    linewidth=1.5,
                    markersize=4,
                    color=color,
                    linestyle=ls,
                    label=combo,
                )
            ax.set_xticks(range(len(present_sizes)))
            ax.set_xticklabels(present_sizes, rotation=30, ha="right")
            ax.set_xlabel("cache size")
            ax.set_ylabel("percentage (%)")
            ax.set_title(title)
            ax.set_ylim(bottom=0)
            ax.grid(True, axis="y", alpha=0.3)

        handles, labels = axes[0].get_legend_handles_labels()
        if handles:
            fig.legend(
                handles,
                labels,
                loc="upper center",
                ncol=min(4, len(handles)),
                fontsize=8,
                frameon=False,
                bbox_to_anchor=(0.5, 1.12),
            )
        fig.suptitle(f"{trace}  |  window={int(window)}", y=1.16, fontsize=12)

        out = outdir / "vs_cache_size" / trace / f"window_{int(window)}.png"
        _save(fig, out)
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--summary",
        type=Path,
        default=Path("results/cp_sweep/summary.csv"),
        help="Path to sweep summary.csv",
    )
    ap.add_argument(
        "--outdir",
        type=Path,
        default=None,
        help="Plots output directory (default: <summary_dir>/plots)",
    )
    ap.add_argument(
        "--traces",
        type=str,
        default="",
        help="Optional comma-separated trace basename filters (substring match)",
    )
    ap.add_argument(
        "--which",
        choices=["both", "vs_window", "vs_cache_size"],
        default="both",
        help="Which plot family to generate",
    )
    args = ap.parse_args()

    summary = args.summary.resolve()
    if not summary.is_file():
        raise SystemExit(f"summary not found: {summary}")

    outdir = args.outdir.resolve() if args.outdir else (summary.parent / "plots")
    outdir.mkdir(parents=True, exist_ok=True)

    df = load_summary(summary)
    if args.traces.strip():
        keys = [t.strip() for t in args.traces.split(",") if t.strip()]
        mask = False
        for k in keys:
            mask = mask | df["trace_stem"].str.contains(re.escape(k), case=False, regex=True)
        df = df[mask].copy()

    if df.empty:
        raise SystemExit("no rows left after filtering")

    print(f"Loaded {len(df)} rows from {summary}")
    print(f"Traces: {df['trace_stem'].nunique()}  sizes: {sorted(df['cache_size'].unique(), key=parse_size_bytes)}")
    print(f"Windows: {sorted(df['interaction_window'].unique())}")
    print(f"Writing plots under {outdir}")

    n1 = n2 = 0
    if args.which in ("both", "vs_window"):
        n1 = plot_vs_window(df, outdir)
        print(f"  vs_window plots:     {n1}")
    if args.which in ("both", "vs_cache_size"):
        n2 = plot_vs_cache_size(df, outdir)
        print(f"  vs_cache_size plots: {n2}")

    print(f"Done. Total figures: {n1 + n2}")


if __name__ == "__main__":
    main()
