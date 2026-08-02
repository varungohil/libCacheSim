#!/usr/bin/env bash
# Run cachesim at 1% of each trace's byte working set (wss_byte).
#
# Cross product: {lru, sieve} × {none, OBL, PG, Mithril}
#
# Uses results/cp_wss/summary.csv (from compute_working_set.py) so WSS is not
# recomputed on every run. Cache size is floor(0.01 * wss_byte) bytes.
#
# Examples:
#   ./scripts/run_wss01_prefetch_sweep.sh --trace-dir ./cloudphysics --jobs 8
#   ./scripts/run_wss01_prefetch_sweep.sh --trace ./cloudphysics/w61.oracleGeneral.bin.zst
#   ./scripts/run_wss01_prefetch_sweep.sh --trace-dir ./cloudphysics \
#       --trace-glob 'w{02,13,19,61,98}.oracleGeneral.bin.zst' --jobs 4

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

CACHESIM="${CACHESIM:-${REPO_ROOT}/_build/bin/cachesim}"
TRACE=""
TRACE_DIR=""
TRACE_TYPE="oracleGeneral"
TRACE_GLOB=""
WSS_CSV="${REPO_ROOT}/results/cp_wss/summary.csv"
WSS_FRAC="0.01"
OUTDIR="${REPO_ROOT}/results/cp_wss01_prefetch"
JOBS=1
NUM_REQ=""
DRY_RUN=0

EVICTION_ALGOS=(lru sieve)
# "none" means no -p flag (baseline without prefetcher)
PREFETCHERS=(none OBL PG Mithril)

usage() {
  cat <<EOF
Usage: $(basename "$0") (--trace PATH | --trace-dir DIR) [options]

Input (one required):
  --trace PATH              Single trace file
  --trace-dir DIR           Run on every matching trace under DIR

Options:
  --trace-type TYPE         Trace type (default: ${TRACE_TYPE})
  --trace-glob GLOB         Only include basenames matching GLOB
  --wss-csv PATH            CSV from compute_working_set.py
                            (default: ${WSS_CSV})
  --wss-frac F              Fraction of wss_byte for cache size (default: ${WSS_FRAC})
  --cachesim PATH           Path to cachesim binary
  --outdir DIR              Output directory (default: ${OUTDIR})
  --eviction LIST           Comma-separated eviction algos (default: lru,sieve)
  --prefetchers LIST        Comma-separated prefetchers; use "none" for no
                            prefetcher (default: none,OBL,PG,Mithril)
  --num-req N               Cap requests (passed to cachesim --num-req)
  --jobs N                  Parallel jobs (default: 1)
  --dry-run                 Print commands only
  -h, --help                Show this help
EOF
}

split_csv() {
  local IFS=,
  # shellcheck disable=SC2206
  local arr=($1)
  printf '%s\n' "${arr[@]}"
}

is_trace_file() {
  local path="$1"
  local base
  base="$(basename "${path}")"
  [[ -f "${path}" ]] || return 1
  case "${base}" in
    *.oracleGeneral.bin.zst|*.oracleGeneral.bin|*.vscsi.zst|*.vscsi|*.csv.zst|*.csv|*.txt.zst|*.txt|*.lcs.zst|*.lcs|*.bin.zst|*.bin)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

discover_traces() {
  local dir="$1"
  local path base
  while IFS= read -r -d '' path; do
    base="$(basename "${path}")"
    if [[ -n "${TRACE_GLOB}" ]] && ! [[ "${base}" == ${TRACE_GLOB} ]]; then
      continue
    fi
    if is_trace_file "${path}"; then
      printf '%s\n' "${path}"
    fi
  done < <(find -L "${dir}" -maxdepth 1 -type f -print0 | sort -z)
}

sanitize() {
  echo "$1" | tr '/,' '__' | tr -cd 'A-Za-z0-9._+-'
}

# Lookup wss_byte for a trace basename in WSS_CSV. Prints integer bytes or empty.
lookup_wss_byte() {
  local basename="$1"
  awk -F, -v t="${basename}" '
    NR == 1 {
      for (i = 1; i <= NF; i++) {
        if ($i == "trace") ti = i
        if ($i == "wss_byte") wi = i
      }
      next
    }
    ti && wi && $ti == t { print $wi; exit }
  ' "${WSS_CSV}"
}

cache_size_from_wss() {
  local wss_byte="$1"
  # floor(frac * wss_byte); require at least 1 byte
  awk -v w="${wss_byte}" -v f="${WSS_FRAC}" 'BEGIN {
    s = int(w * f)
    if (s < 1) s = 1
    print s
  }'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --trace) TRACE="$2"; shift 2 ;;
    --trace-dir) TRACE_DIR="$2"; shift 2 ;;
    --trace-type) TRACE_TYPE="$2"; shift 2 ;;
    --trace-glob) TRACE_GLOB="$2"; shift 2 ;;
    --wss-csv) WSS_CSV="$2"; shift 2 ;;
    --wss-frac) WSS_FRAC="$2"; shift 2 ;;
    --cachesim) CACHESIM="$2"; shift 2 ;;
    --outdir) OUTDIR="$2"; shift 2 ;;
    --eviction)
      mapfile -t EVICTION_ALGOS < <(split_csv "$2")
      shift 2
      ;;
    --prefetchers)
      mapfile -t PREFETCHERS < <(split_csv "$2")
      shift 2
      ;;
    --num-req) NUM_REQ="$2"; shift 2 ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "${TRACE}" && -z "${TRACE_DIR}" ]]; then
  echo "error: --trace or --trace-dir is required" >&2
  usage >&2
  exit 1
fi
if [[ -n "${TRACE}" && -n "${TRACE_DIR}" ]]; then
  echo "error: use only one of --trace or --trace-dir" >&2
  exit 1
fi
if [[ -n "${TRACE}" && ! -f "${TRACE}" ]]; then
  echo "error: trace not found: ${TRACE}" >&2
  exit 1
fi
if [[ -n "${TRACE_DIR}" && ! -d "${TRACE_DIR}" ]]; then
  echo "error: trace dir not found: ${TRACE_DIR}" >&2
  exit 1
fi
if [[ ! -x "${CACHESIM}" ]]; then
  echo "error: cachesim not found/executable: ${CACHESIM}" >&2
  exit 1
fi
if [[ ! -f "${WSS_CSV}" ]]; then
  echo "error: WSS CSV not found: ${WSS_CSV}" >&2
  echo "Run: ./scripts/compute_working_set.py --trace-dir ./cloudphysics --outdir ./results/cp_wss" >&2
  exit 1
fi

TRACES=()
if [[ -n "${TRACE}" ]]; then
  TRACES=("${TRACE}")
else
  mapfile -t TRACES < <(discover_traces "${TRACE_DIR}")
  if [[ "${#TRACES[@]}" -eq 0 ]]; then
    echo "error: no traces found in ${TRACE_DIR}" >&2
    exit 1
  fi
fi

mkdir -p "${OUTDIR}/logs"
SUMMARY_CSV="${OUTDIR}/summary.csv"
PROGRESS_FILE="${OUTDIR}/progress.txt"
SUMMARY_HEADER="trace,trace_type,eviction,prefetcher,cache_size,wss_byte,wss_frac,miss_ratio,byte_miss_ratio,n_req,log_path,status"

if [[ ! -f "${SUMMARY_CSV}" ]]; then
  echo "${SUMMARY_HEADER}" > "${SUMMARY_CSV}"
fi

run_one() {
  local trace_path="$1"
  local ttype="$2"
  local eviction="$3"
  local prefetcher="$4"
  local size="$5"
  local wss_byte="$6"

  local trace_tag
  trace_tag="$(sanitize "$(basename "${trace_path}")")"
  local pf_tag="${prefetcher}"
  [[ "${pf_tag}" == "none" ]] && pf_tag="noprefetch"
  local tag
  tag="${trace_tag}__$(sanitize "${eviction}")__$(sanitize "${pf_tag}")__$(sanitize "${size}")"
  local log="${OUTDIR}/logs/${tag}.log"
  local ofile="${OUTDIR}/logs/${tag}.cachesim.out"

  local cmd=("${CACHESIM}" "${trace_path}" "${ttype}" "${eviction}" "${size}" -o "${ofile}")
  if [[ "${prefetcher}" != "none" ]]; then
    cmd+=(-p "${prefetcher}")
  fi
  if [[ -n "${NUM_REQ}" ]]; then
    cmd+=(--num-req="${NUM_REQ}")
  fi

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[dry-run]'
    printf ' %q' "${cmd[@]}"
    printf '\n'
    return 0
  fi

  local status="ok"
  set +e
  "${cmd[@]}" >"${log}" 2>&1
  local rc=$?
  set -e
  if [[ ${rc} -ne 0 ]]; then
    status="fail:${rc}"
  fi

  local miss_ratio="" byte_miss_ratio="" n_req=""
  local result_line
  result_line="$(grep -E 'miss ratio' "${log}" | grep -v 'interval miss' | tail -n 1 || true)"
  if [[ -n "${result_line}" ]]; then
    local cleaned
    cleaned="$(sed 's/byte miss ratio [0-9.]*,\{0,1\}[[:space:]]*//' <<<"${result_line}")"
    miss_ratio="$(sed -n 's/.*miss ratio \([0-9.]*\).*/\1/p' <<<"${cleaned}" | head -n1)"
    if grep -q 'byte miss ratio' <<<"${result_line}"; then
      byte_miss_ratio="$(sed -n 's/.*byte miss ratio \([0-9.]*\).*/\1/p' <<<"${result_line}" | head -n1)"
    fi
    n_req="$(sed -n 's/.*, *\([0-9][0-9]*\) req,.*/\1/p' <<<"${result_line}" | head -n1)"
  fi

  local row
  row="$(basename "${trace_path}"),${ttype},${eviction},${prefetcher},${size},${wss_byte},${WSS_FRAC},${miss_ratio},${byte_miss_ratio},${n_req},${log},${status}"
  if command -v flock >/dev/null 2>&1; then
    (
      flock 9
      echo "${row}" >> "${SUMMARY_CSV}"
    ) 9>>"${SUMMARY_CSV}"
  else
    echo "${row}" >> "${SUMMARY_CSV}"
  fi

  echo "${tag} -> ${status}" >> "${PROGRESS_FILE}"
  echo "${tag} -> ${status}"
}

# Build job list: trace|type|eviction|prefetcher|size|wss_byte
JOB_LIST=()
MISSING_WSS=0
for trace_path in "${TRACES[@]}"; do
  base="$(basename "${trace_path}")"
  wss_byte="$(lookup_wss_byte "${base}" || true)"
  if [[ -z "${wss_byte}" ]]; then
    echo "WARNING: no wss_byte for ${base} in ${WSS_CSV}; skipping" >&2
    MISSING_WSS=$((MISSING_WSS + 1))
    continue
  fi
  size="$(cache_size_from_wss "${wss_byte}")"
  for eviction in "${EVICTION_ALGOS[@]}"; do
    for prefetcher in "${PREFETCHERS[@]}"; do
      JOB_LIST+=("${trace_path}|${TRACE_TYPE}|${eviction}|${prefetcher}|${size}|${wss_byte}")
    done
  done
done

if [[ "${#JOB_LIST[@]}" -eq 0 ]]; then
  echo "error: no jobs planned (${MISSING_WSS} traces missing WSS)" >&2
  exit 1
fi

TOTAL="${#JOB_LIST[@]}"
echo "Planning ${TOTAL} runs over $((TOTAL / (${#EVICTION_ALGOS[@]} * ${#PREFETCHERS[@]}))) trace(s)"
echo "  eviction:    ${EVICTION_ALGOS[*]}"
echo "  prefetchers: ${PREFETCHERS[*]}"
echo "  wss_frac:    ${WSS_FRAC} of wss_byte"
echo "  wss_csv:     ${WSS_CSV}"
echo "  outdir:      ${OUTDIR}"
echo "  jobs:        ${JOBS}"
if [[ "${MISSING_WSS}" -gt 0 ]]; then
  echo "  skipped:     ${MISSING_WSS} (missing WSS)"
fi
echo

export -f run_one sanitize lookup_wss_byte cache_size_from_wss
export CACHESIM OUTDIR NUM_REQ DRY_RUN SUMMARY_CSV PROGRESS_FILE WSS_FRAC WSS_CSV

if [[ "${JOBS}" -le 1 ]]; then
  idx=0
  for spec in "${JOB_LIST[@]}"; do
    idx=$((idx + 1))
    IFS='|' read -r trace_path ttype eviction prefetcher size wss_byte <<<"${spec}"
    echo "[${idx}/${TOTAL}] $(basename "${trace_path}") | ${eviction} + ${prefetcher} @ ${size}B (1% of ${wss_byte})"
    run_one "${trace_path}" "${ttype}" "${eviction}" "${prefetcher}" "${size}" "${wss_byte}"
  done
else
  printf '%s\n' "${JOB_LIST[@]}" \
    | xargs -P "${JOBS}" -n 1 -I {} bash -c '
        IFS="|" read -r trace_path ttype eviction prefetcher size wss_byte <<<"{}"
        run_one "$trace_path" "$ttype" "$eviction" "$prefetcher" "$size" "$wss_byte"
      '
fi

echo
echo "Done. Summary: ${SUMMARY_CSV}"
echo "Logs:          ${OUTDIR}/logs/"
