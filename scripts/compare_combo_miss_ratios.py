#!/usr/bin/env python3
"""
Compare two eviction+prefetcher combos from a sweep summary.csv.

Reports per-trace and aggregate differences in miss_ratio / byte_miss_ratio,
plus win counts (how many traces one combo has a higher ratio than the other).

Combo format: eviction+prefetcher, e.g. lru+none, sieve+OBL, lru+Mithril

Examples:
  ./scripts/compare_combo_miss_ratios.py \\
      --summary ./results/cp_wss01_prefetch/summary.csv \\
      --a lru+none --b sieve+none

  ./scripts/compare_combo_miss_ratios.py \\
      --summary ./results/cp_wss01_prefetch/summary.csv \\
      --a lru+OBL --b sieve+OBL --csv ./results/cp_wss01_prefetch/lru_obl_vs_sieve_obl.csv
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Combo:
    eviction: str
    prefetcher: str

    @classmethod
    def parse(cls, s: str) -> Combo:
        parts = s.split("+", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise argparse.ArgumentTypeError(
                f"combo must be eviction+prefetcher, got: {s!r}"
            )
        return cls(eviction=parts[0].strip().lower(), prefetcher=parts[1].strip())

    def __str__(self) -> str:
        return f"{self.eviction}+{self.prefetcher}"


def _norm_prefetch(p: str) -> str:
    p = p.strip()
    if p.lower() in {"none", "noprefetch", ""}:
        return "none"
    return p


def load_combo_rows(
    summary: Path, combo: Combo
) -> dict[str, dict[str, float | str]]:
    """Map trace -> {miss_ratio, byte_miss_ratio, ...} for one combo (ok rows only)."""
    out: dict[str, dict[str, float | str]] = {}
    with summary.open(newline="") as f:
        reader = csv.DictReader(f)
        required = {"trace", "eviction", "prefetcher", "miss_ratio", "byte_miss_ratio"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            missing = required - set(reader.fieldnames or [])
            raise SystemExit(f"summary missing columns: {sorted(missing)}")

        for row in reader:
            if row.get("status", "ok") not in ("", "ok"):
                continue
            if row["eviction"].strip().lower() != combo.eviction:
                continue
            if _norm_prefetch(row["prefetcher"]) != _norm_prefetch(combo.prefetcher):
                continue
            mr = (row.get("miss_ratio") or "").strip()
            bmr = (row.get("byte_miss_ratio") or "").strip()
            if not mr or not bmr:
                continue
            try:
                miss = float(mr)
                byte_miss = float(bmr)
            except ValueError:
                continue
            # If duplicates (e.g. re-runs), keep the last successful row.
            out[row["trace"]] = {
                "miss_ratio": miss,
                "byte_miss_ratio": byte_miss,
                "cache_size": row.get("cache_size", ""),
                "n_req": row.get("n_req", ""),
            }
    return out


def _fmt_pct(x: float) -> str:
    return f"{100.0 * x:+.4f} pp"


def _summarize_deltas(deltas: list[float], label: str, a: Combo, b: Combo) -> None:
    a_higher = sum(1 for d in deltas if d > 0)  # a - b > 0 => a higher
    b_higher = sum(1 for d in deltas if d < 0)
    tied = sum(1 for d in deltas if d == 0)
    mean_d = statistics.mean(deltas)
    med_d = statistics.median(deltas)

    print(f"\n{label}")
    print(f"  mean  ({a} - {b}): {mean_d:+.6f}  ({_fmt_pct(mean_d)})")
    print(f"  median({a} - {b}): {med_d:+.6f}  ({_fmt_pct(med_d)})")
    print(f"  traces where {a} higher: {a_higher}")
    print(f"  traces where {b} higher: {b_higher}")
    print(f"  traces tied:             {tied}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--summary",
        type=Path,
        default=Path("results/cp_wss01_prefetch/summary.csv"),
        help="Sweep summary.csv",
    )
    ap.add_argument(
        "--a",
        type=Combo.parse,
        required=True,
        help="First combo, e.g. lru+none",
    )
    ap.add_argument(
        "--b",
        type=Combo.parse,
        required=True,
        help="Second combo, e.g. sieve+OBL",
    )
    ap.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Optional per-trace delta CSV output path",
    )
    ap.add_argument(
        "--top",
        type=int,
        default=10,
        help="Show this many largest |miss_ratio| deltas (0 to disable)",
    )
    args = ap.parse_args()

    if not args.summary.is_file():
        print(f"error: summary not found: {args.summary}", file=sys.stderr)
        return 1

    rows_a = load_combo_rows(args.summary, args.a)
    rows_b = load_combo_rows(args.summary, args.b)
    common = sorted(set(rows_a) & set(rows_b))
    only_a = sorted(set(rows_a) - set(rows_b))
    only_b = sorted(set(rows_b) - set(rows_a))

    print(f"summary: {args.summary}")
    print(f"A: {args.a}  ({len(rows_a)} ok traces)")
    print(f"B: {args.b}  ({len(rows_b)} ok traces)")
    print(f"common traces: {len(common)}")
    if only_a:
        print(f"  only in A: {len(only_a)}")
    if only_b:
        print(f"  only in B: {len(only_b)}")

    if not common:
        print("error: no overlapping traces with both combos", file=sys.stderr)
        return 1

    per_trace: list[dict[str, object]] = []
    d_miss: list[float] = []
    d_byte: list[float] = []

    for trace in common:
        ra, rb = rows_a[trace], rows_b[trace]
        dm = float(ra["miss_ratio"]) - float(rb["miss_ratio"])
        db = float(ra["byte_miss_ratio"]) - float(rb["byte_miss_ratio"])
        d_miss.append(dm)
        d_byte.append(db)
        per_trace.append(
            {
                "trace": trace,
                "miss_a": ra["miss_ratio"],
                "miss_b": rb["miss_ratio"],
                "miss_diff_a_minus_b": dm,
                "byte_miss_a": ra["byte_miss_ratio"],
                "byte_miss_b": rb["byte_miss_ratio"],
                "byte_miss_diff_a_minus_b": db,
                "n_req": ra.get("n_req") or rb.get("n_req"),
                "cache_size": ra.get("cache_size") or rb.get("cache_size"),
            }
        )

    _summarize_deltas(d_miss, "miss_ratio", args.a, args.b)
    _summarize_deltas(d_byte, "byte_miss_ratio", args.a, args.b)

    if args.top > 0:
        ranked = sorted(
            per_trace, key=lambda r: abs(float(r["miss_diff_a_minus_b"])), reverse=True
        )
        print(f"\nTop {min(args.top, len(ranked))} |miss_ratio| deltas ({args.a} - {args.b}):")
        print(
            f"  {'trace':40s}  {'miss_a':>8s}  {'miss_b':>8s}  {'d_miss':>10s}  "
            f"{'bm_a':>8s}  {'bm_b':>8s}  {'d_byte':>10s}"
        )
        for r in ranked[: args.top]:
            print(
                f"  {str(r['trace']):40s}  "
                f"{float(r['miss_a']):8.4f}  {float(r['miss_b']):8.4f}  "
                f"{float(r['miss_diff_a_minus_b']):+10.4f}  "
                f"{float(r['byte_miss_a']):8.4f}  {float(r['byte_miss_b']):8.4f}  "
                f"{float(r['byte_miss_diff_a_minus_b']):+10.4f}"
            )

    if args.csv is not None:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "trace",
            "miss_a",
            "miss_b",
            "miss_diff_a_minus_b",
            "byte_miss_a",
            "byte_miss_b",
            "byte_miss_diff_a_minus_b",
            "n_req",
            "cache_size",
            "combo_a",
            "combo_b",
        ]
        with args.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in per_trace:
                row = dict(r)
                row["combo_a"] = str(args.a)
                row["combo_b"] = str(args.b)
                w.writerow(row)
        print(f"\nWrote {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
