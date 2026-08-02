#!/usr/bin/env python3
"""
Plot global intensity vs trace clock_time from cachesim logs.

Parses the block emitted by prefetch_interaction_print:
  global intensity vs clock_time (bucket_sec=..., clock_origin=...):
    clock_start,n_req,n_pf_evict_miss,n_ev_useless,intensity_pct
    ...

Y values are rewritten as
  100 * (n_pf_evict_miss + n_ev_useless) / n_req_trace
using the whole-trace request count from the summary (not per-bucket n_req),
so each point is that bucket's contribution to global intensity.

Example:
  ./scripts/plot_global_intensity_vs_clock.py \\
      --summary ./results/cp_sweep_intensity/summary.csv \\
      --outdir ./results/cp_sweep_intensity/plots/intensity_vs_clock \\
      --traces w35 --sizes 64mb --windows 1000
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

_HEADER_RE = re.compile(
    r"global intensity vs clock_time\s*\(bucket_sec=(\d+),\s*clock_origin=(-?\d+)\)",
    re.IGNORECASE,
)
_ROW_RE = re.compile(r"^\s*(-?\d+),(\d+),(\d+),(\d+),([0-9.]+)\s*$")


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


def parse_intensity_series(text: str) -> tuple[int, int, pd.DataFrame] | None:
    """Return (bucket_sec, clock_origin, df) or None."""
    lines = text.splitlines()
    start = None
    bucket_sec = 60
    clock_origin = 0
    for i, line in enumerate(lines):
        m = _HEADER_RE.search(line)
        if m:
            bucket_sec = int(m.group(1))
            clock_origin = int(m.group(2))
            start = i + 1
            break
    if start is None:
        return None

    rows: list[dict] = []
    for line in lines[start:]:
        if not line.strip():
            if rows:
                break
            continue
        if "clock_start" in line and "intensity_pct" in line:
            continue
        if line.strip().startswith("prefetch") or line.strip().startswith("evict"):
            break
        m = _ROW_RE.match(line)
        if not m:
            if rows:
                break
            continue
        rows.append(
            {
                "clock_start": int(m.group(1)),
                "n_req": int(m.group(2)),
                "n_pf_evict_miss": int(m.group(3)),
                "n_ev_useless": int(m.group(4)),
                "intensity_pct": float(m.group(5)),
            }
        )
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["clock_rel"] = df["clock_start"] - clock_origin
    return bucket_sec, clock_origin, df


def load_summary(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["interaction_window"] = pd.to_numeric(df["interaction_window"], errors="coerce")
    df["cache_size"] = df["cache_size"].astype(str)
    df["trace_stem"] = df["trace"].astype(str).map(lambda t: Path(t).name)
    df["combo"] = df["eviction"].astype(str) + "+" + df["prefetcher"].astype(str)
    return df


def _style_for_combos(combos: list[str]) -> dict:
    cmap = plt.get_cmap("tab20")
    styles = ["-", "--", "-.", ":"]
    out = {}
    for i, c in enumerate(sorted(combos)):
        out[c] = (cmap(i % 20), styles[i % len(styles)])
    return out


def plot_group(
    g: pd.DataFrame,
    summary_dir: Path,
    out_path: Path,
    title: str,
) -> bool:
    series = []
    for _, row in g.iterrows():
        log = _resolve_log_path(str(row.get("log_path", "")), summary_dir)
        if log is None:
            continue
        try:
            text = log.read_text(errors="replace")
        except OSError:
            continue
        parsed = parse_intensity_series(text)
        if parsed is None:
            continue
        _bucket_sec, _clock_origin, sdf = parsed
        # Prefer whole-trace n_req from the summary; fall back to sum of buckets.
        total_n_req = pd.to_numeric(row.get("n_req"), errors="coerce")
        if pd.isna(total_n_req) or total_n_req <= 0:
            total_n_req = float(sdf["n_req"].sum())
        else:
            total_n_req = float(total_n_req)
        if total_n_req <= 0:
            continue
        sdf = sdf.copy()
        # Intensity as % of entire-trace requests (not per-bucket n_req).
        sdf["intensity_pct_of_trace"] = (
            100.0
            * (sdf["n_pf_evict_miss"] + sdf["n_ev_useless"]).astype(float)
            / total_n_req
        )
        series.append((row["combo"], sdf, total_n_req))

    if not series:
        return False

    styles = _style_for_combos([c for c, _, _ in series])
    fig, ax = plt.subplots(figsize=(9, 4.5))
    for combo, sdf, _total in series:
        color, ls = styles[combo]
        x = sdf["clock_rel"] / 3600.0  # hours since origin
        ax.plot(
            x,
            sdf["intensity_pct_of_trace"],
            label=combo,
            color=color,
            linestyle=ls,
            linewidth=1.4,
        )
    ax.set_xlabel("trace clock time (hours since first request)")
    ax.set_ylabel("global intensity (% of trace requests)")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8, loc="best")
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--traces", type=str, default="", help="Comma substrings")
    ap.add_argument("--sizes", type=str, default="", help="Comma sizes")
    ap.add_argument("--windows", type=str, default="", help="Comma windows")
    ap.add_argument(
        "--combos",
        type=str,
        default="",
        help="Comma eviction+prefetcher filters, e.g. sieve+Mithril",
    )
    args = ap.parse_args()

    df = load_summary(args.summary)
    df = df[df["interaction_window"].fillna(0) > 0].copy()

    if args.traces:
        toks = [t.strip() for t in args.traces.split(",") if t.strip()]
        df = df[df["trace_stem"].apply(lambda s: any(t in s for t in toks))]
    if args.sizes:
        want = {s.strip().lower() for s in args.sizes.split(",") if s.strip()}
        df = df[df["cache_size"].str.lower().isin(want)]
    if args.windows:
        want_w = {int(float(w.strip())) for w in args.windows.split(",") if w.strip()}
        df = df[df["interaction_window"].astype(int).isin(want_w)]
    if args.combos:
        want_c = {c.strip() for c in args.combos.split(",") if c.strip()}
        df = df[df["combo"].isin(want_c)]

    if df.empty:
        print("no matching runs")
        return

    summary_dir = args.summary.parent
    n_ok = 0
    n_skip = 0
    for (trace, size, window), g in df.groupby(
        ["trace_stem", "cache_size", "interaction_window"], sort=True
    ):
        out = args.outdir / str(trace) / f"cache_{size}__w{int(window)}.png"
        title = f"{trace} | size={size} | W={int(window)}"
        if plot_group(g, summary_dir, out, title):
            n_ok += 1
            print(f"wrote {out}")
        else:
            n_skip += 1
            print(
                f"skip {trace} size={size} W={window}: "
                "no intensity-vs-clock series in logs "
                "(re-run with intensity-time-bucket>0)"
            )
    print(f"done: {n_ok} plots, {n_skip} skipped")


if __name__ == "__main__":
    main()
