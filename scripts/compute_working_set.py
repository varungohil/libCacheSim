#!/usr/bin/env python3
"""
Compute working-set size (WSS) for oracleGeneral traces.

For each trace:
  - wss_obj   = number of unique object IDs
  - wss_byte  = sum of first-seen object sizes (matches traceAnalyzer)
  - n_req / n_req_byte for context

Supports uncompressed `.oracleGeneral.bin` and `.zst` (via `zstd -dc`).

Examples:
  ./scripts/compute_working_set.py --trace ./cloudphysics/w61.oracleGeneral.bin.zst
  ./scripts/compute_working_set.py --trace-dir ./cloudphysics --jobs 8 \\
      --outdir ./results/cp_wss
"""

from __future__ import annotations

import argparse
import csv
import struct
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

# oracleGeneral: uint32 clock, uint64 obj_id, uint32 obj_size, int64 next_access
_RECORD_FMT = "<IQIq"
_RECORD_SIZE = struct.calcsize(_RECORD_FMT)
assert _RECORD_SIZE == 24

_TRACE_SUFFIXES = (
    ".oracleGeneral.bin.zst",
    ".oracleGeneral.bin",
    ".bin.zst",
    ".bin",
)


@dataclass
class TraceWSS:
    trace: str
    n_req: int
    n_req_byte: int
    wss_obj: int
    wss_byte: int
    mean_obj_size_req: float
    mean_obj_size_obj: float
    cold_miss_ratio: float
    byte_cold_miss_ratio: float
    start_clock: int | None
    end_clock: int | None
    elapsed_sec: float


def _open_trace_bytes(path: Path):
    """Return a readable binary stream for path (decompresses .zst)."""
    if path.suffix == ".zst" or str(path).endswith(".zst"):
        proc = subprocess.Popen(
            ["zstd", "-dc", "--", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        return proc.stdout, proc
    return path.open("rb"), None


def compute_wss(path: Path, num_req: int | None = None) -> TraceWSS:
    """Scan one oracleGeneral trace and compute WSS metrics."""
    t0 = time.perf_counter()
    stream, proc = _open_trace_bytes(path)
    # first-seen size per object id
    first_size: dict[int, int] = {}
    n_req = 0
    n_req_byte = 0
    wss_byte = 0
    start_clock: int | None = None
    end_clock: int | None = None
    unpack = struct.unpack
    try:
        while True:
            if num_req is not None and n_req >= num_req:
                break
            buf = stream.read(_RECORD_SIZE)
            if len(buf) < _RECORD_SIZE:
                break
            clock, obj_id, obj_size, _next = unpack(_RECORD_FMT, buf)
            if obj_size == 0:
                continue
            if start_clock is None:
                start_clock = clock
            end_clock = clock
            n_req += 1
            n_req_byte += obj_size
            if obj_id not in first_size:
                first_size[obj_id] = obj_size
                wss_byte += obj_size
    finally:
        if proc is not None:
            # Stop early (--num-req) or normal EOF: terminate decompressor.
            proc.kill()
            proc.wait()
            if n_req == 0 and proc.returncode not in (0, -9):
                err = ""
                if proc.stderr is not None:
                    err = proc.stderr.read().decode(errors="replace")
                raise RuntimeError(f"zstd failed on {path}: rc={proc.returncode} {err}")
        else:
            stream.close()

    wss_obj = len(first_size)
    mean_req = (n_req_byte / n_req) if n_req else 0.0
    mean_obj = (wss_byte / wss_obj) if wss_obj else 0.0
    cold = (wss_obj / n_req) if n_req else 0.0
    byte_cold = (wss_byte / n_req_byte) if n_req_byte else 0.0
    return TraceWSS(
        trace=path.name,
        n_req=n_req,
        n_req_byte=n_req_byte,
        wss_obj=wss_obj,
        wss_byte=wss_byte,
        mean_obj_size_req=mean_req,
        mean_obj_size_obj=mean_obj,
        cold_miss_ratio=cold,
        byte_cold_miss_ratio=byte_cold,
        start_clock=start_clock,
        end_clock=end_clock,
        elapsed_sec=time.perf_counter() - t0,
    )


def _worker(args: tuple[str, int | None]) -> dict:
    path_str, num_req = args
    result = compute_wss(Path(path_str), num_req=num_req)
    return asdict(result)


def discover_traces(trace_dir: Path, recursive: bool) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    traces: list[Path] = []
    for p in sorted(trace_dir.glob(pattern)):
        if not p.is_file():
            continue
        name = p.name
        if any(name.endswith(sfx) for sfx in _TRACE_SUFFIXES):
            traces.append(p)
    return traces


def _human_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(x) < 1024.0:
            return f"{x:.2f} {unit}"
        x /= 1024.0
    return f"{x:.2f} PiB"


def _print_row(r: TraceWSS) -> None:
    print(
        f"{r.trace}: n_req={r.n_req:,}  wss_obj={r.wss_obj:,}  "
        f"wss_byte={_human_bytes(r.wss_byte)} ({r.wss_byte:,})  "
        f"n_req_byte={_human_bytes(r.n_req_byte)}  "
        f"cold={r.cold_miss_ratio:.4f}/{r.byte_cold_miss_ratio:.4f}  "
        f"[{r.elapsed_sec:.1f}s]"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--trace", type=Path, help="Single oracleGeneral trace file")
    g.add_argument("--trace-dir", type=Path, help="Directory of traces")
    p.add_argument("--recursive", action="store_true", help="Recurse into --trace-dir")
    p.add_argument("--glob", dest="trace_glob", default=None, help="Basename glob filter, e.g. 'w*.zst'")
    p.add_argument("--num-req", type=int, default=None, help="Cap requests per trace")
    p.add_argument("--jobs", type=int, default=1, help="Parallel workers (default: 1)")
    p.add_argument(
        "--outdir",
        type=Path,
        default=None,
        help="Write summary.csv here (default: print only; if set, also write CSV)",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="CSV output path (default: <outdir>/summary.csv or stdout skipped)",
    )
    args = p.parse_args()

    if args.trace is not None:
        traces = [args.trace.resolve()]
        if not traces[0].is_file():
            print(f"error: trace not found: {traces[0]}", file=sys.stderr)
            return 1
    else:
        trace_dir = args.trace_dir.resolve()
        if not trace_dir.is_dir():
            print(f"error: not a directory: {trace_dir}", file=sys.stderr)
            return 1
        traces = discover_traces(trace_dir, recursive=args.recursive)
        if args.trace_glob:
            import fnmatch

            traces = [t for t in traces if fnmatch.fnmatch(t.name, args.trace_glob)]
        if not traces:
            print(f"error: no traces found under {trace_dir}", file=sys.stderr)
            return 1

    out_csv: Path | None = args.output
    if out_csv is None and args.outdir is not None:
        args.outdir.mkdir(parents=True, exist_ok=True)
        out_csv = args.outdir / "summary.csv"
    elif args.outdir is not None:
        args.outdir.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "trace",
        "n_req",
        "n_req_byte",
        "wss_obj",
        "wss_byte",
        "mean_obj_size_req",
        "mean_obj_size_obj",
        "cold_miss_ratio",
        "byte_cold_miss_ratio",
        "start_clock",
        "end_clock",
        "elapsed_sec",
    ]

    results: list[dict] = []
    work = [(str(t), args.num_req) for t in traces]
    print(f"Computing WSS for {len(work)} trace(s) with jobs={args.jobs}", flush=True)

    if args.jobs <= 1:
        for path_str, num_req in work:
            row = asdict(compute_wss(Path(path_str), num_req=num_req))
            results.append(row)
            _print_row(TraceWSS(**row))
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            futs = {ex.submit(_worker, item): item[0] for item in work}
            for fut in as_completed(futs):
                path_str = futs[fut]
                try:
                    row = fut.result()
                except Exception as e:
                    print(f"error: {path_str}: {e}", file=sys.stderr)
                    continue
                results.append(row)
                _print_row(TraceWSS(**row))

    results.sort(key=lambda r: r["trace"])

    if out_csv is not None:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(results)
        print(f"Wrote {out_csv} ({len(results)} rows)", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
