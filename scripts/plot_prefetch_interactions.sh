#!/usr/bin/env bash
# Wrapper around plot_prefetch_interactions.py
#
# Example (all traces from cp_sweep):
#   ./scripts/plot_prefetch_interactions.sh
#
# Subset:
#   ./scripts/plot_prefetch_interactions.sh --traces w02,w35,w54

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SUMMARY="${SUMMARY:-${REPO_ROOT}/results/cp_sweep/summary.csv}"
OUTDIR="${OUTDIR:-${REPO_ROOT}/results/cp_sweep/plots}"

exec python3 "${SCRIPT_DIR}/plot_prefetch_interactions.py" \
  --summary "${SUMMARY}" \
  --outdir "${OUTDIR}" \
  "$@"
