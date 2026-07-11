#!/usr/bin/env bash
# Sweep cachesim over traces × eviction × prefetcher × interaction-window × cache-size.
#
# Single trace:
#   ./scripts/run_prefetch_interaction_sweep.sh \
#     --trace ./cache_dataset/tutorials/w89.oracleGeneral.bin.zst \
#     --trace-type oracleGeneral
#
# All traces in a directory:
#   ./scripts/run_prefetch_interaction_sweep.sh \
#     --trace-dir ./cloudphysics \
#     --trace-type oracleGeneral \
#     --jobs 4 --outdir ./results/cp_sweep

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

CACHESIM="${CACHESIM:-${REPO_ROOT}/_build/bin/cachesim}"
TRACE=""
TRACE_DIR=""
TRACE_TYPE="oracleGeneral"
TRACE_GLOB=""          # optional basename glob, e.g. 'w*.oracleGeneral.bin.zst'
RECURSIVE=0
OUTDIR="${REPO_ROOT}/results/prefetch_interaction_sweep"
JOBS=1
NUM_REQ=""             # empty => full trace
DRY_RUN=0
ALL_EVICTION=0

# Defaults: common replacement policies (override with --eviction or --all-eviction)
EVICTION_ALGOS=(lru fifo lfu arc clock sieve s3fifo lecar twoq qdlp)
PREFETCHERS=(Mithril PG OBL)
CACHE_SIZES=(16mb 64mb 256mb 1gb)
# 0 disables interaction tracking for that run
INTERACTION_WINDOWS=(0 1 10 100 1000)

usage() {
  cat <<EOF
Usage: $(basename "$0") (--trace PATH | --trace-dir DIR) [options]

Input (one required):
  --trace PATH              Single trace file
  --trace-dir DIR           Run on every trace file under DIR

Options:
  --trace-type TYPE         Trace type (default: ${TRACE_TYPE})
                            Use "auto" to infer from each filename
  --trace-glob GLOB         Only include basenames matching GLOB (e.g. 'w*.zst')
  --recursive               Recurse into subdirectories of --trace-dir
  --cachesim PATH           Path to cachesim binary (default: ${CACHESIM})
  --outdir DIR              Output directory (default: ${OUTDIR})
  --eviction LIST           Comma-separated eviction algos
  --all-eviction            Use a broad set of eviction algorithms
  --prefetchers LIST        Comma-separated prefetchers (default: Mithril,PG,OBL)
  --sizes LIST              Comma-separated cache sizes (default: 16mb,64mb,256mb,1gb)
  --windows LIST            Comma-separated interaction windows (default: 0,1,10,100,1000)
  --num-req N               Cap requests (passed to cachesim --num-req)
  --jobs N                  Parallel jobs (default: 1)
  --dry-run                 Print commands only
  -h, --help                Show this help

Notes:
  - Prefetchers: Mithril, PG, OBL
  - interaction-window=0 still runs the prefetcher but disables interaction stats
  - With --all-eviction, Belady/BeladySize are added only for oracleGeneral/lcs traces
  - Discovered files match: *.zst *.bin *.vscsi *.csv *.txt *.lcs (and uncompressed variants)
EOF
}

split_csv() {
  local IFS=,
  # shellcheck disable=SC2206
  local arr=($1)
  printf '%s\n' "${arr[@]}"
}

infer_trace_type() {
  local path="$1"
  local base
  base="$(basename "${path}")"
  case "${base}" in
    *oracleGeneral*) echo "oracleGeneral" ;;
    *.vscsi*) echo "vscsi" ;;
    *.csv*) echo "csv" ;;
    *.txt) echo "txt" ;;
    *.lcs*) echo "lcs" ;;
    *.twr*) echo "twr" ;;
    *) echo "${TRACE_TYPE}" ;;
  esac
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
  local -a found=()
  local path base

  if [[ "${RECURSIVE}" -eq 1 ]]; then
    while IFS= read -r -d '' path; do
      found+=("${path}")
    done < <(find -L "${dir}" -type f -print0 | sort -z)
  else
    while IFS= read -r -d '' path; do
      found+=("${path}")
    done < <(find -L "${dir}" -maxdepth 1 -type f -print0 | sort -z)
  fi

  for path in "${found[@]}"; do
    base="$(basename "${path}")"
    if [[ -n "${TRACE_GLOB}" ]] && ! [[ "${base}" == ${TRACE_GLOB} ]]; then
      continue
    fi
    if is_trace_file "${path}"; then
      printf '%s\n' "${path}"
    fi
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --trace) TRACE="$2"; shift 2 ;;
    --trace-dir) TRACE_DIR="$2"; shift 2 ;;
    --trace-type) TRACE_TYPE="$2"; shift 2 ;;
    --trace-glob) TRACE_GLOB="$2"; shift 2 ;;
    --recursive) RECURSIVE=1; shift ;;
    --cachesim) CACHESIM="$2"; shift 2 ;;
    --outdir) OUTDIR="$2"; shift 2 ;;
    --eviction)
      mapfile -t EVICTION_ALGOS < <(split_csv "$2")
      shift 2
      ;;
    --all-eviction) ALL_EVICTION=1; shift ;;
    --prefetchers)
      mapfile -t PREFETCHERS < <(split_csv "$2")
      shift 2
      ;;
    --sizes)
      mapfile -t CACHE_SIZES < <(split_csv "$2")
      shift 2
      ;;
    --windows)
      mapfile -t INTERACTION_WINDOWS < <(split_csv "$2")
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
  echo "Build first, or set --cachesim / CACHESIM=..." >&2
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

BASE_EVICTION_ALGOS=("${EVICTION_ALGOS[@]}")
if [[ "${ALL_EVICTION}" -eq 1 ]]; then
  BASE_EVICTION_ALGOS=(
    lru fifo lfu lfuda arc clock clockpro sieve s3fifo twoq
    slru lecar cacheus qdlp lhd gdsf hyperbolic wtinyLFU random
  )
fi

mkdir -p "${OUTDIR}/logs"
SUMMARY_CSV="${OUTDIR}/summary.csv"
PROGRESS_FILE="${OUTDIR}/progress.txt"

# CSV header
if [[ ! -f "${SUMMARY_CSV}" ]]; then
  echo "trace,trace_type,eviction,prefetcher,cache_size,interaction_window,miss_ratio,byte_miss_ratio,n_req,n_prefetch,n_prefetch_then_evict,n_evict,n_evict_then_prefetch,log_path,status" \
    > "${SUMMARY_CSV}"
fi

sanitize() {
  # turn path-ish tokens into filename-safe strings
  echo "$1" | tr '/,' '__' | tr -cd 'A-Za-z0-9._+-'
}

eviction_algos_for_trace() {
  local trace_path="$1"
  local ttype="$2"
  local -a algos=("${BASE_EVICTION_ALGOS[@]}")
  if [[ "${ALL_EVICTION}" -eq 1 ]]; then
    if [[ "${ttype}" == "oracleGeneral" || "${ttype}" == "lcs" ||
          "${trace_path}" == *oracleGeneral* || "${trace_path}" == *.lcs* ]]; then
      algos+=(belady beladySize)
    fi
  fi
  printf '%s\n' "${algos[@]}"
}

run_one() {
  local trace_path="$1"
  local ttype="$2"
  local eviction="$3"
  local prefetcher="$4"
  local size="$5"
  local window="$6"

  local trace_tag
  trace_tag="$(sanitize "$(basename "${trace_path}")")"
  local tag
  tag="${trace_tag}__$(sanitize "${eviction}")__$(sanitize "${prefetcher}")__$(sanitize "${size}")__w$(sanitize "${window}")"
  local log="${OUTDIR}/logs/${tag}.log"
  local ofile="${OUTDIR}/logs/${tag}.cachesim.out"

  local params=""
  if [[ "${window}" != "0" ]]; then
    params="interaction-window=${window}"
  fi

  local cmd=("${CACHESIM}" "${trace_path}" "${ttype}" "${eviction}" "${size}"
    -p "${prefetcher}" -o "${ofile}")
  if [[ -n "${params}" ]]; then
    cmd+=(--prefetch-params="${params}")
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
  local n_prefetch="" n_pf_evict="" n_evict="" n_ev_pf=""

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

  if grep -q 'prefetch interaction' "${log}"; then
    n_prefetch="$(awk '/prefetches:/{for(i=1;i<=NF;i++) if($i ~ /^[0-9]+$/){print $i; exit}}' "${log}" | tail -n1)"
    n_pf_evict="$(awk '/prefetch.*evict \(unused\):/{for(i=1;i<=NF;i++) if($i ~ /^[0-9]+$/){print $i; exit}}' "${log}" | tail -n1)"
    n_evict="$(awk '/^[[:space:]]*evictions:/{for(i=1;i<=NF;i++) if($i ~ /^[0-9]+$/){print $i; exit}}' "${log}" | tail -n1)"
    n_ev_pf="$(awk '/evict.*prefetch:/{for(i=1;i<=NF;i++) if($i ~ /^[0-9]+$/){print $i; exit}}' "${log}" | tail -n1)"
  fi

  local row
  row="$(basename "${trace_path}"),${ttype},${eviction},${prefetcher},${size},${window},${miss_ratio},${byte_miss_ratio},${n_req},${n_prefetch},${n_pf_evict},${n_evict},${n_ev_pf},${log},${status}"
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

# Build job list: trace|type|eviction|prefetcher|size|window
JOB_LIST=()
for trace_path in "${TRACES[@]}"; do
  local_ttype="${TRACE_TYPE}"
  if [[ "${TRACE_TYPE}" == "auto" ]]; then
    local_ttype="$(infer_trace_type "${trace_path}")"
  fi
  mapfile -t trace_eviction < <(eviction_algos_for_trace "${trace_path}" "${local_ttype}")
  for eviction in "${trace_eviction[@]}"; do
    for prefetcher in "${PREFETCHERS[@]}"; do
      for size in "${CACHE_SIZES[@]}"; do
        for window in "${INTERACTION_WINDOWS[@]}"; do
          JOB_LIST+=("${trace_path}|${local_ttype}|${eviction}|${prefetcher}|${size}|${window}")
        done
      done
    done
  done
done

TOTAL="${#JOB_LIST[@]}"
echo "Planning ${TOTAL} runs over ${#TRACES[@]} trace(s)"
echo "  traces:      ${#TRACES[@]}"
if [[ "${#TRACES[@]}" -le 10 ]]; then
  for t in "${TRACES[@]}"; do
    echo "               - ${t}"
  done
else
  echo "               (showing first 5)"
  for t in "${TRACES[@]:0:5}"; do
    echo "               - ${t}"
  done
  echo "               - ..."
fi
echo "  eviction:    ${BASE_EVICTION_ALGOS[*]}$([[ ${ALL_EVICTION} -eq 1 ]] && echo ' (+belady on oracle traces)')"
echo "  prefetchers: ${PREFETCHERS[*]}"
echo "  sizes:       ${CACHE_SIZES[*]}"
echo "  windows:     ${INTERACTION_WINDOWS[*]}"
echo "  outdir:      ${OUTDIR}"
echo "  jobs:        ${JOBS}"
echo

export -f run_one sanitize split_csv infer_trace_type is_trace_file eviction_algos_for_trace
export CACHESIM TRACE_TYPE OUTDIR NUM_REQ DRY_RUN SUMMARY_CSV PROGRESS_FILE ALL_EVICTION
export BASE_EVICTION_ALGOS

if [[ "${JOBS}" -le 1 ]]; then
  idx=0
  for spec in "${JOB_LIST[@]}"; do
    idx=$((idx + 1))
    IFS='|' read -r trace_path ttype eviction prefetcher size window <<<"${spec}"
    echo "[${idx}/${TOTAL}] $(basename "${trace_path}") | ${eviction} + ${prefetcher} @ ${size} w=${window}"
    run_one "${trace_path}" "${ttype}" "${eviction}" "${prefetcher}" "${size}" "${window}"
  done
else
  printf '%s\n' "${JOB_LIST[@]}" \
    | xargs -P "${JOBS}" -n 1 -I {} bash -c '
        IFS="|" read -r trace_path ttype eviction prefetcher size window <<<"{}"
        run_one "$trace_path" "$ttype" "$eviction" "$prefetcher" "$size" "$window"
      '
fi

echo
echo "Done. Summary: ${SUMMARY_CSV}"
echo "Logs:          ${OUTDIR}/logs/"
