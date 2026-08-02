#!/usr/bin/env python3
"""
Find traces with a cyclic miss-ratio ordering among three combos.

For metrics miss_ratio and byte_miss_ratio, report traces where:
  metric(b) < metric(a)  and  metric(c) < metric(b)  and  metric(a) < metric(c)

i.e. a is worse than b, b is worse than c, yet c is worse than a.

Combo format: eviction+prefetcher, e.g. lru+none, sieve+OBL, lru+Mithril

Examples:
  ./scripts/find_combo_cycle_traces.py \\
      --summary ./results/cp_wss01_prefetch/summary.csv \\
      --a lru+none --b sieve+none --c fifo+none

  ./scripts/find_combo_cycle_traces.py \\
      --summary ./results/cp_wss01_prefetch/summary.csv \\
      --a lru+OBL --b sieve+OBL --c lru+Mithril \\
      --csv ./results/cp_wss01_prefetch/cycle_traces.csv
"""

from __future__ import annotations

import argparse
import csv
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


def _is_cycle(va: float, vb: float, vc: float) -> bool:
    """True iff b < a, c < b, and a < c."""
    return vb < va and vc < vb and va < vc


def _print_cycle_table(
    label: str,
    rows: list[dict[str, object]],
    a: Combo,
    b: Combo,
    c: Combo,
    key_a: str,
    key_b: str,
    key_c: str,
) -> None:
    print(f"\n{label}: {len(rows)} traces with {b} < {a}, {c} < {b}, {a} < {c}")
    if not rows:
        return
    print(
        f"  {'trace':40s}  "
        f"{'a':>10s}  {'b':>10s}  {'c':>10s}"
    )
    print(f"  {'':40s}  {str(a):>10s}  {str(b):>10s}  {str(c):>10s}")
    for r in rows:
        print(
            f"  {str(r['trace']):40s}  "
            f"{float(r[key_a]):10.6f}  {float(r[key_b]):10.6f}  {float(r[key_c]):10.6f}"
        )


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
        help="Second combo, e.g. sieve+none",
    )
    ap.add_argument(
        "--c",
        type=Combo.parse,
        required=True,
        help="Third combo, e.g. fifo+none",
    )
    ap.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Optional CSV with cycle traces for both metrics",
    )
    args = ap.parse_args()

    if not args.summary.is_file():
        print(f"error: summary not found: {args.summary}", file=sys.stderr)
        return 1

    rows_a = load_combo_rows(args.summary, args.a)
    rows_b = load_combo_rows(args.summary, args.b)
    rows_c = load_combo_rows(args.summary, args.c)
    common = sorted(set(rows_a) & set(rows_b) & set(rows_c))

    print(f"summary: {args.summary}")
    print(f"A: {args.a}  ({len(rows_a)} ok traces)")
    print(f"B: {args.b}  ({len(rows_b)} ok traces)")
    print(f"C: {args.c}  ({len(rows_c)} ok traces)")
    print(f"common traces: {len(common)}")

    if not common:
        print("error: no overlapping traces with all three combos", file=sys.stderr)
        return 1

    miss_cycles: list[dict[str, object]] = []
    byte_cycles: list[dict[str, object]] = []

    for trace in common:
        ra, rb, rc = rows_a[trace], rows_b[trace], rows_c[trace]
        ma = float(ra["miss_ratio"])
        mb = float(rb["miss_ratio"])
        mc = float(rc["miss_ratio"])
        ba = float(ra["byte_miss_ratio"])
        bb = float(rb["byte_miss_ratio"])
        bc = float(rc["byte_miss_ratio"])
        base = {
            "trace": trace,
            "miss_a": ma,
            "miss_b": mb,
            "miss_c": mc,
            "byte_miss_a": ba,
            "byte_miss_b": bb,
            "byte_miss_c": bc,
            "n_req": ra.get("n_req") or rb.get("n_req") or rc.get("n_req"),
            "cache_size": ra.get("cache_size")
            or rb.get("cache_size")
            or rc.get("cache_size"),
        }
        if _is_cycle(ma, mb, mc):
            miss_cycles.append(base)
        if _is_cycle(ba, bb, bc):
            byte_cycles.append(base)

    _print_cycle_table(
        "miss_ratio",
        miss_cycles,
        args.a,
        args.b,
        args.c,
        "miss_a",
        "miss_b",
        "miss_c",
    )
    _print_cycle_table(
        "byte_miss_ratio",
        byte_cycles,
        args.a,
        args.b,
        args.c,
        "byte_miss_a",
        "byte_miss_b",
        "byte_miss_c",
    )

    both = {r["trace"] for r in miss_cycles} & {r["trace"] for r in byte_cycles}
    print(f"\nTraces in both miss_ratio and byte_miss_ratio cycles: {len(both)}")
    for t in sorted(both):
        print(f"  {t}")

    if args.csv is not None:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "metric",
            "trace",
            "miss_a",
            "miss_b",
            "miss_c",
            "byte_miss_a",
            "byte_miss_b",
            "byte_miss_c",
            "n_req",
            "cache_size",
            "combo_a",
            "combo_b",
            "combo_c",
        ]
        with args.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for metric, rows in (
                ("miss_ratio", miss_cycles),
                ("byte_miss_ratio", byte_cycles),
            ):
                for r in rows:
                    row = dict(r)
                    row["metric"] = metric
                    row["combo_a"] = str(args.a)
                    row["combo_b"] = str(args.b)
                    row["combo_c"] = str(args.c)
                    w.writerow(row)
        print(f"\nWrote {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
