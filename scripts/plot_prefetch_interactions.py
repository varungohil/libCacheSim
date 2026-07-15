#!/usr/bin/env python3
"""
Plot prefetch interaction percentages from a sweep summary.csv.

Plot families:
  1) vs_window/          — for each (trace, cache_size): 2×3 panel grid
  2) vs_cache_size/      — for each (trace, interaction_window): 2×3 panel grid
  3) useful/vs_window/   — dedicated evict→useful % vs window
  4) useful/vs_cache_size/ — dedicated evict→useful % vs cache size

Combined 3×3 layout:
  Top:    evict→prefetch | evict→useful | evict→useless   (all / evictions)
  Mid:    prefetch→evict | prefetch→evict→miss | miss-distance histogram
          (/ prefetches)   (/ prefetches)        (from run logs)
  Bottom: global intensity = (pf→evict→miss + evict→useless) / n_req

Histogram panel uses one representative run per combo:
  vs_window     → largest interaction_window in the figure
  vs_cache_size → largest cache size in the figure
  Parsed from log_path (log2 bins of miss_vtime − evict_vtime).

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
import numpy as np
import pandas as pd

_HIST_HEADER_RE = re.compile(
    r"prefetch.*evict.*miss distance hist", re.IGNORECASE
)
_HIST_BIN_RE = re.compile(
    r"\[\s*(\d+)\s*,\s*(?:\+inf|(\d+))\s*\)\s*:\s*(\d+)", re.IGNORECASE
)
_THEN_MISS_RE = re.compile(r"then miss:\s*(\d+)", re.IGNORECASE)
_NO_DEMAND_RE = re.compile(r"no later demand:\s*(\d+)", re.IGNORECASE)
_REINSERT_HIT_RE = re.compile(r"reinserted then hit:\s*(\d+)", re.IGNORECASE)


def parse_size_bytes(size: str) -> int:
    s = str(size).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(kb|mb|gb|tb|b)?", s)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2) or "b"
    mult = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}[unit]
    return int(val * mult)


def _resolve_log_path(log_path: str, summary_dir: Path) -> Path | None:
    if not isinstance(log_path, str) or not log_path.strip():
        return None
    p = Path(log_path.strip())
    candidates = []
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(Path.cwd() / p)
        candidates.append(summary_dir / p)
        # strip leading ./results/... relative to summary parent
        candidates.append(summary_dir.parent.parent / p)
        candidates.append(summary_dir / p.name)
        candidates.append(summary_dir / "logs" / p.name)
    for c in candidates:
        try:
            if c.is_file():
                return c.resolve()
        except OSError:
            continue
    return None


def parse_miss_hist_from_log(text: str) -> list[tuple[int, int | None, int]]:
    """Return [(lo, hi_or_None, count), ...] from printed log2 histogram."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _HIST_HEADER_RE.search(line):
            start = i + 1
            break
    if start is None:
        return []
    bins: list[tuple[int, int | None, int]] = []
    for line in lines[start:]:
        if not line.strip():
            break
        if line.strip().startswith("prefetch") or line.strip().startswith("evict"):
            break
        if "(empty)" in line:
            return []
        m = _HIST_BIN_RE.search(line)
        if not m:
            # stop at first non-bin line after header
            if bins:
                break
            continue
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else None
        cnt = int(m.group(3))
        bins.append((lo, hi, cnt))
    return bins


def _enrich_from_logs(df: pd.DataFrame, summary_dir: Path) -> pd.DataFrame:
    """Attach miss hist + fill miss split columns from log text when needed."""
    hists: list = []
    miss_from_log: list = []
    nodemand_from_log: list = []
    hit_from_log: list = []

    for _, row in df.iterrows():
        path = _resolve_log_path(str(row.get("log_path", "")), summary_dir)
        text = ""
        if path is not None:
            try:
                text = path.read_text(errors="replace")
            except OSError:
                text = ""
        hists.append(parse_miss_hist_from_log(text) if text else [])
        if text:
            mm = _THEN_MISS_RE.search(text)
            nd = _NO_DEMAND_RE.search(text)
            hh = _REINSERT_HIT_RE.search(text)
            miss_from_log.append(int(mm.group(1)) if mm else np.nan)
            nodemand_from_log.append(int(nd.group(1)) if nd else np.nan)
            hit_from_log.append(int(hh.group(1)) if hh else np.nan)
        else:
            miss_from_log.append(np.nan)
            nodemand_from_log.append(np.nan)
            hit_from_log.append(np.nan)

    df = df.copy()
    df["miss_hist"] = hists
    # Prefer CSV columns; fall back to log parse.
    for col, parsed in [
        ("n_prefetch_then_evict_then_miss", miss_from_log),
        ("n_prefetch_then_evict_no_demand", nodemand_from_log),
        ("n_prefetch_then_evict_then_hit", hit_from_log),
    ]:
        if col not in df.columns:
            df[col] = parsed
        else:
            df[col] = df[col].where(df[col].notna(), parsed)
    return df


def load_summary(path: Path, enrich_logs: bool = True) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df[
        df["trace"]
        .astype(str)
        .str.endswith((".zst", ".bin", ".vscsi", ".csv", ".txt", ".lcs"))
    ].copy()
    df = df[~df["trace"].astype(str).str.startswith("request_counts")].copy()

    for c in [
        "interaction_window",
        "n_req",
        "n_prefetch",
        "n_prefetch_then_evict",
        "n_prefetch_then_evict_then_miss",
        "n_prefetch_then_evict_no_demand",
        "n_prefetch_then_evict_then_hit",
        "n_evict",
        "n_evict_then_prefetch",
        "n_evict_then_useful_prefetch",
        "n_evict_then_useless_prefetch",
    ]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df[df["status"].astype(str).str.startswith("ok") | df["status"].isna()].copy()
    # Exclude w=0 (no tracking) and short windows w=1, w=10.
    df = df[~df["interaction_window"].isin([0, 1, 10])].copy()

    if "miss_hist" not in df.columns:
        df["miss_hist"] = [[] for _ in range(len(df))]
    for c in [
        "n_prefetch_then_evict_then_miss",
        "n_prefetch_then_evict_no_demand",
        "n_prefetch_then_evict_then_hit",
    ]:
        if c not in df.columns:
            df[c] = pd.NA

    if enrich_logs:
        df = _enrich_from_logs(df, path.parent)

    has_split = (
        "n_evict_then_useful_prefetch" in df.columns
        and df["n_evict_then_useful_prefetch"].notna().any()
    )
    if not has_split:
        df["n_evict_then_useful_prefetch"] = pd.NA
        df["n_evict_then_useless_prefetch"] = pd.NA

    # Recompute percentages after possible log enrichment.
    has_pf_evict_split = df["n_prefetch_then_evict_then_miss"].notna().any()
    has_hist = any(isinstance(h, list) and len(h) > 0 for h in df["miss_hist"])

    df["pct_pf_evict"] = (
        100.0 * df["n_prefetch_then_evict"] / df["n_prefetch"].where(df["n_prefetch"] > 0)
    )
    # Absolute miss rate among prefetches (aligned with prefetch→evict panel).
    df["pct_pf_evict_miss"] = (
        100.0
        * df["n_prefetch_then_evict_then_miss"]
        / df["n_prefetch"].where(df["n_prefetch"] > 0)
    )
    df["pct_ev_pf"] = (
        100.0 * df["n_evict_then_prefetch"] / df["n_evict"].where(df["n_evict"] > 0)
    )
    df["pct_ev_pf_useful"] = (
        100.0
        * df["n_evict_then_useful_prefetch"]
        / df["n_evict"].where(df["n_evict"] > 0)
    )
    df["pct_ev_pf_useless"] = (
        100.0
        * df["n_evict_then_useless_prefetch"]
        / df["n_evict"].where(df["n_evict"] > 0)
    )
    df["pct_useful_of_ev_pf"] = (
        100.0
        * df["n_evict_then_useful_prefetch"]
        / df["n_evict_then_prefetch"].where(df["n_evict_then_prefetch"] > 0)
    )
    # Harmful interactions per request: premature unused-prefetch miss +
    # useless evict→reprefetch.
    df["pct_global_intensity"] = (
        100.0
        * (
            df["n_prefetch_then_evict_then_miss"].fillna(0)
            + df["n_evict_then_useless_prefetch"].fillna(0)
        )
        / df["n_req"].where(df["n_req"] > 0)
    )
    # Only defined when at least one harmful counter is present.
    both_missing = (
        df["n_prefetch_then_evict_then_miss"].isna()
        & df["n_evict_then_useless_prefetch"].isna()
    )
    df.loc[both_missing, "pct_global_intensity"] = pd.NA
    df["combo"] = df["eviction"].astype(str) + "+" + df["prefetcher"].astype(str)
    df["cache_size"] = df["cache_size"].astype(str)
    df["trace_stem"] = df["trace"].astype(str).map(lambda t: Path(t).name)

    key = [
        "trace_stem",
        "eviction",
        "prefetcher",
        "cache_size",
        "interaction_window",
    ]
    df["_has_split"] = df["n_evict_then_useful_prefetch"].notna().astype(int)
    df["_has_split"] = df["_has_split"] + df[
        "n_prefetch_then_evict_then_miss"
    ].notna().astype(int)
    df["_has_hist"] = df["miss_hist"].map(lambda h: int(isinstance(h, list) and len(h) > 0))
    df["_has_split"] = df["_has_split"] + df["_has_hist"]
    df = (
        df.sort_values("_has_split")
        .drop_duplicates(subset=key, keep="last")
        .drop(columns=["_has_split", "_has_hist"])
        .reset_index(drop=True)
    )

    df.attrs["has_useful_split"] = bool(has_split)
    df.attrs["has_pf_evict_split"] = bool(has_pf_evict_split)
    df.attrs["has_hist"] = bool(has_hist)
    df.attrs["summary_dir"] = path.parent
    return df


def _finalize_metrics(df: pd.DataFrame, summary_dir: Path) -> pd.DataFrame:
    """Enrich from logs (once filtered) and recompute derived columns."""
    df = _enrich_from_logs(df, summary_dir)
    df["pct_pf_evict"] = (
        100.0 * df["n_prefetch_then_evict"] / df["n_prefetch"].where(df["n_prefetch"] > 0)
    )
    df["pct_pf_evict_miss"] = (
        100.0
        * df["n_prefetch_then_evict_then_miss"]
        / df["n_prefetch"].where(df["n_prefetch"] > 0)
    )
    df["pct_global_intensity"] = (
        100.0
        * (
            df["n_prefetch_then_evict_then_miss"].fillna(0)
            + df["n_evict_then_useless_prefetch"].fillna(0)
        )
        / df["n_req"].where(df["n_req"] > 0)
    )
    both_missing = (
        df["n_prefetch_then_evict_then_miss"].isna()
        & df["n_evict_then_useless_prefetch"].isna()
    )
    df.loc[both_missing, "pct_global_intensity"] = pd.NA
    df.attrs["has_pf_evict_split"] = bool(df["n_prefetch_then_evict_then_miss"].notna().any())
    df.attrs["has_hist"] = any(
        isinstance(h, list) and len(h) > 0 for h in df["miss_hist"]
    )
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


def _plot_lines_vs_window(ax, g: pd.DataFrame, ycol: str, styles: dict) -> None:
    for combo, cg in g.groupby("combo"):
        cg = cg.sort_values("interaction_window").dropna(subset=[ycol])
        if cg.empty:
            continue
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
    ax.set_ylim(bottom=0)
    ax.grid(True, which="both", alpha=0.3)


def _plot_lines_vs_cache(
    ax, g: pd.DataFrame, ycol: str, styles: dict, present_sizes: list[str], size_to_x: dict
) -> None:
    for combo, cg in g.groupby("combo"):
        cg = cg.copy()
        cg["x"] = cg["cache_size"].map(size_to_x)
        cg = cg.dropna(subset=["x", ycol]).sort_values("x")
        if cg.empty:
            continue
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
    ax.set_ylim(bottom=0)
    ax.grid(True, axis="y", alpha=0.3)


def _pick_hist_rows(g: pd.DataFrame, mode: str) -> pd.DataFrame:
    """One row per combo for the histogram panel."""
    rows = []
    for combo, cg in g.groupby("combo"):
        if mode == "window":
            cg = cg.sort_values("interaction_window")
        else:
            cg = cg.copy()
            cg["_sz"] = cg["cache_size"].map(parse_size_bytes)
            cg = cg.sort_values("_sz")
        # Prefer a row that actually has histogram bins.
        with_hist = cg[cg["miss_hist"].map(lambda h: isinstance(h, list) and len(h) > 0)]
        pick = with_hist.iloc[-1] if not with_hist.empty else cg.iloc[-1]
        rows.append(pick)
    return pd.DataFrame(rows)


def _plot_miss_hist(ax, g: pd.DataFrame, styles: dict, mode: str) -> None:
    """Step/line plot of miss-distance histogram for each combo."""
    picks = _pick_hist_rows(g, mode)
    any_data = False
    for _, row in picks.iterrows():
        hist = row.get("miss_hist", [])
        if not isinstance(hist, list) or not hist:
            continue
        any_data = True
        combo = row["combo"]
        color, ls = styles[combo]
        xs = [b[0] for b in hist]
        ys = [b[2] for b in hist]
        ax.plot(
            xs,
            ys,
            marker="o",
            linewidth=1.5,
            markersize=4,
            color=color,
            linestyle=ls,
            label=combo,
            drawstyle="steps-mid",
        )
    ax.set_xscale("log")
    ax.set_xlabel("evict→miss distance (requests)")
    ax.set_ylabel("count")
    ax.set_ylim(bottom=0)
    ax.grid(True, which="both", alpha=0.3)
    if mode == "window":
        w = int(picks["interaction_window"].max()) if not picks.empty else 0
        ax.set_title(f"pf→evict→miss distance hist\n(window={w})")
    else:
        # Largest cache size used for the picks
        if not picks.empty:
            picks = picks.copy()
            picks["_sz"] = picks["cache_size"].map(parse_size_bytes)
            sz = picks.loc[picks["_sz"].idxmax(), "cache_size"]
        else:
            sz = "?"
        ax.set_title(f"pf→evict→miss distance hist\n(cache={sz})")
    if not any_data:
        ax.text(
            0.5,
            0.5,
            "no histogram in logs\n(re-run sweep)",
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=9,
            color="0.4",
        )


def _add_legend(fig, ax) -> None:
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        fig.legend(
            handles,
            labels,
            loc="upper center",
            ncol=min(4, len(handles)),
            fontsize=8,
            frameon=False,
            bbox_to_anchor=(0.5, 1.06),
        )


def _plot_combined_grid(
    g: pd.DataFrame,
    styles: dict,
    *,
    x_mode: str,
    present_sizes: list[str] | None = None,
    size_to_x: dict | None = None,
) -> plt.Figure:
    """
    3×3 grid:
      top:    evict→prefetch | useful | useless
      mid:    prefetch→evict | pf→evict→miss | histogram
      bottom: global intensity | (unused) | (unused)
    """
    fig, axes = plt.subplots(3, 3, figsize=(14.5, 12.0))

    top = [
        ("pct_ev_pf", "evict→prefetch / evictions"),
        ("pct_ev_pf_useful", "evict→useful prefetch / evictions"),
        ("pct_ev_pf_useless", "evict→useless prefetch / evictions"),
    ]
    mid_lines = [
        ("pct_pf_evict", "prefetch→evict / prefetches"),
        ("pct_pf_evict_miss", "prefetch→evict→miss / prefetches"),
    ]

    for ax, (ycol, title) in zip(axes[0], top):
        if x_mode == "window":
            _plot_lines_vs_window(ax, g, ycol, styles)
        else:
            _plot_lines_vs_cache(ax, g, ycol, styles, present_sizes, size_to_x)
        ax.set_title(title)

    for ax, (ycol, title) in zip(axes[1, :2], mid_lines):
        if x_mode == "window":
            _plot_lines_vs_window(ax, g, ycol, styles)
        else:
            _plot_lines_vs_cache(ax, g, ycol, styles, present_sizes, size_to_x)
        ax.set_title(title)

    _plot_miss_hist(axes[1, 2], g, styles, mode=x_mode)

    if x_mode == "window":
        _plot_lines_vs_window(axes[2, 0], g, "pct_global_intensity", styles)
    else:
        _plot_lines_vs_cache(
            axes[2, 0], g, "pct_global_intensity", styles, present_sizes, size_to_x
        )
    axes[2, 0].set_title(
        "global intensity\n(pf→evict→miss + ev→useless) / requests"
    )
    axes[2, 1].axis("off")
    axes[2, 2].axis("off")

    _add_legend(fig, axes[0, 0])
    return fig


def plot_vs_window(df: pd.DataFrame, outdir: Path) -> int:
    """Combined 3×3 plots: one per (trace, cache_size)."""
    n = 0
    for (trace, cache_size), g in df.groupby(["trace_stem", "cache_size"], sort=True):
        combos = g["combo"].unique().tolist()
        styles = _style_for_combos(combos)
        fig = _plot_combined_grid(g, styles, x_mode="window")
        fig.suptitle(f"{trace}  |  cache={cache_size}", y=1.08, fontsize=12)
        _save(fig, outdir / "vs_window" / trace / f"cache_{cache_size}.png")
        n += 1
    return n


def plot_vs_cache_size(df: pd.DataFrame, outdir: Path) -> int:
    """Combined 3×3 plots: one per (trace, interaction_window)."""
    n = 0
    present_sizes = sorted(df["cache_size"].unique(), key=parse_size_bytes)
    size_to_x = {s: i for i, s in enumerate(present_sizes)}

    for (trace, window), g in df.groupby(["trace_stem", "interaction_window"], sort=True):
        combos = g["combo"].unique().tolist()
        styles = _style_for_combos(combos)
        fig = _plot_combined_grid(
            g,
            styles,
            x_mode="cache",
            present_sizes=present_sizes,
            size_to_x=size_to_x,
        )
        fig.suptitle(f"{trace}  |  window={int(window)}", y=1.08, fontsize=12)
        _save(fig, outdir / "vs_cache_size" / trace / f"window_{int(window)}.png")
        n += 1
    return n


def plot_useful_vs_window(df: pd.DataFrame, outdir: Path) -> int:
    """Dedicated single-panel useful plots vs interaction window."""
    n = 0
    for (trace, cache_size), g in df.groupby(["trace_stem", "cache_size"], sort=True):
        if g["pct_ev_pf_useful"].isna().all():
            continue
        combos = g["combo"].unique().tolist()
        styles = _style_for_combos(combos)
        fig, axes = plt.subplots(1, 2, figsize=(11, 4), sharex=True)

        _plot_lines_vs_window(axes[0], g, "pct_ev_pf_useful", styles)
        axes[0].set_title("evict→useful / evictions")

        _plot_lines_vs_window(axes[1], g, "pct_useful_of_ev_pf", styles)
        axes[1].set_title("useful / evict→prefetch events")

        _add_legend(fig, axes[0])
        fig.suptitle(f"{trace}  |  cache={cache_size}", y=1.12, fontsize=12)
        _save(fig, outdir / "useful" / "vs_window" / trace / f"cache_{cache_size}.png")
        n += 1
    return n


def plot_useful_vs_cache_size(df: pd.DataFrame, outdir: Path) -> int:
    """Dedicated single-panel useful plots vs cache size."""
    n = 0
    present_sizes = sorted(df["cache_size"].unique(), key=parse_size_bytes)
    size_to_x = {s: i for i, s in enumerate(present_sizes)}

    for (trace, window), g in df.groupby(["trace_stem", "interaction_window"], sort=True):
        if g["pct_ev_pf_useful"].isna().all():
            continue
        combos = g["combo"].unique().tolist()
        styles = _style_for_combos(combos)
        fig, axes = plt.subplots(1, 2, figsize=(11, 4), sharex=True)

        _plot_lines_vs_cache(axes[0], g, "pct_ev_pf_useful", styles, present_sizes, size_to_x)
        axes[0].set_title("evict→useful / evictions")

        _plot_lines_vs_cache(
            axes[1], g, "pct_useful_of_ev_pf", styles, present_sizes, size_to_x
        )
        axes[1].set_title("useful / evict→prefetch events")

        _add_legend(fig, axes[0])
        fig.suptitle(f"{trace}  |  window={int(window)}", y=1.12, fontsize=12)
        _save(
            fig, outdir / "useful" / "vs_cache_size" / trace / f"window_{int(window)}.png"
        )
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
        choices=["both", "vs_window", "vs_cache_size", "useful"],
        default="both",
        help="Which plot family to generate (both includes dedicated useful plots)",
    )
    args = ap.parse_args()

    summary = args.summary.resolve()
    if not summary.is_file():
        raise SystemExit(f"summary not found: {summary}")

    outdir = args.outdir.resolve() if args.outdir else (summary.parent / "plots")
    outdir.mkdir(parents=True, exist_ok=True)

    df = load_summary(summary, enrich_logs=False)
    if args.traces.strip():
        keys = [t.strip() for t in args.traces.split(",") if t.strip()]
        mask = False
        for k in keys:
            mask = mask | df["trace_stem"].str.contains(
                re.escape(k), case=False, regex=True
            )
        df = df[mask].copy()

    if df.empty:
        raise SystemExit("no rows left after filtering")

    summary_dir = Path(df.attrs.get("summary_dir", summary.parent))
    df = _finalize_metrics(df, summary_dir)
    df.attrs["has_useful_split"] = bool(
        "n_evict_then_useful_prefetch" in df.columns
        and df["n_evict_then_useful_prefetch"].notna().any()
    )

    has_split = bool(df.attrs.get("has_useful_split", False))
    has_pf = bool(df.attrs.get("has_pf_evict_split", False))
    has_hist = bool(df.attrs.get("has_hist", False))
    print(f"Loaded {len(df)} rows from {summary}")
    print(
        f"Traces: {df['trace_stem'].nunique()}  "
        f"sizes: {sorted(df['cache_size'].unique(), key=parse_size_bytes)}"
    )
    print(f"Windows: {sorted(df['interaction_window'].unique())}")
    print(f"Useful/useless split columns present: {has_split}")
    print(f"prefetch→evict miss columns present: {has_pf}")
    print(f"Miss-distance histograms found in logs: {has_hist}")
    if not has_hist:
        print(
            "NOTE: histogram panel will be empty until sweep logs include "
            "'prefetch→evict→miss distance hist'."
        )
    print(f"Writing plots under {outdir}")

    n1 = n2 = n3 = n4 = 0
    if args.which in ("both", "vs_window"):
        n1 = plot_vs_window(df, outdir)
        print(f"  vs_window plots:            {n1}")
    if args.which in ("both", "vs_cache_size"):
        n2 = plot_vs_cache_size(df, outdir)
        print(f"  vs_cache_size plots:        {n2}")
    if args.which in ("both", "useful") and has_split:
        n3 = plot_useful_vs_window(df, outdir)
        n4 = plot_useful_vs_cache_size(df, outdir)
        print(f"  useful/vs_window plots:     {n3}")
        print(f"  useful/vs_cache_size plots: {n4}")
    elif args.which == "useful" and not has_split:
        raise SystemExit(
            "Cannot plot useful/: summary.csv lacks n_evict_then_useful_prefetch"
        )

    print(f"Done. Total figures: {n1 + n2 + n3 + n4}")


if __name__ == "__main__":
    main()
