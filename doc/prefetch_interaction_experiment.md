# Prefetch–Eviction Interaction Experiment

This document describes an experiment that measures how often **prefetching** and **cache eviction** interfere with each other under libCacheSim, using CloudPhysics block traces and several eviction / prefetcher combinations.

## Motivation

libCacheSim’s built-in prefetchers (`Mithril`, `PG`, `OBL`) insert prefetched objects **directly into the main cache** (not a side buffer). Prefetches therefore compete with demand traffic for capacity and can:

1. **Prefetch → evict:** a prefetched object is evicted before any demand hit (wasted prefetch / cache pollution).
2. **Evict → prefetch:** an object is evicted and then soon prefetched again (thrashing / churn).

These effects are usually invisible in miss-ratio alone. This experiment adds counters for both interactions and sweeps policies, cache sizes, and time windows to quantify them.

## Metrics

All times are in **request virtual time** (`cache->n_req`), not wall clock.

Given an interaction window \(W\) (in requests):

| Counter | Definition |
|---|---|
| `n_prefetch` | Prefetched inserts |
| `n_prefetch_then_evict` | Prefetched objects evicted **unused** within \(W\) requests |
| `n_prefetch_then_evict_then_miss` | Subset of prefetch→evict with a later **demand miss** for that object |
| `n_prefetch_then_evict_no_demand` | Subset with **no later demand** request for that object |
| `n_prefetch_then_evict_then_hit` | Subset brought back (e.g. re-prefetch) and **demand-hit** with no intervening demand miss |
| `n_evict` | Evictions |
| `n_evict_then_prefetch` | Evicted objects prefetched again within \(W\) requests |
| `n_evict_then_useful_prefetch` | Subset of evict→prefetch that later got a **demand hit** |
| `n_evict_then_useless_prefetch` | Subset of evict→prefetch that was **never demand-hit** (unused eviction, overwritten re-prefetch, or still unused at end of run) |

Invariant after each run finishes printing:

\[
\texttt{n\_prefetch\_then\_evict}
= \texttt{n\_prefetch\_then\_evict\_then\_miss}
+ \texttt{n\_prefetch\_then\_evict\_no\_demand}
+ \texttt{n\_prefetch\_then\_evict\_then\_hit}
\]

\[
\texttt{n\_evict\_then\_prefetch}
= \texttt{n\_evict\_then\_useful\_prefetch}
+ \texttt{n\_evict\_then\_useless\_prefetch}
\]

For each prefetch→evict→miss, distance \(\texttt{miss\_vtime} - \texttt{evict\_vtime}\) (request counts) is recorded in a **log2 histogram** printed at the end of the run (`[2^i, 2^{i+1})` bins).

Reported percentages:

\[
\text{prefetch→evict \%}
= 100 \cdot \frac{\texttt{n\_prefetch\_then\_evict}}{\texttt{n\_prefetch}}
\]

\[
\text{evict→prefetch \%}
= 100 \cdot \frac{\texttt{n\_evict\_then\_prefetch}}{\texttt{n\_evict}}
\]

\[
\text{evict→useful \%}
= 100 \cdot \frac{\texttt{n\_evict\_then\_useful\_prefetch}}{\texttt{n\_evict}},
\quad
\text{evict→useless \%}
= 100 \cdot \frac{\texttt{n\_evict\_then\_useless\_prefetch}}{\texttt{n\_evict}}
\]

Notes:

- Prefetch→evict only counts if there was **no demand hit** between prefetch and eviction.
- Prefetch→evict→miss / no-demand / reinserted-hit classify **only** those unused-prefetch evictions; a later demand for an absent object is a miss and attributes **all** outstanding watches for that object.
- Evict→useful / useless classify **only** the re-prefetches that formed an evict→prefetch event.
- Mithril’s second-chance reinsert is **not** counted as a permanent eviction.
- `interaction-window=0` disables tracking (prefetcher still runs).

Implementation: `libCacheSim/cache/prefetch/prefetchInteraction.c`, hooked from `cache_find_base` / `cache_evict_base` and each prefetcher’s insert path. Configure via:

```bash
--prefetch-params="interaction-window=100"
# or separately:
--prefetch-params="prefetch-evict-window=1000,evict-prefetch-window=10"
```

## Experimental setup

### Workload

- **Dataset:** CloudPhysics (`2015_cloudphysics`), oracleGeneral format
- **Traces:** 106 files under `./cloudphysics/` (`w01` … `w106`)
- **Scale:** ~2.11B requests total (≈3.3M–216M per trace; median ≈10.6M)

### Factors swept

| Factor | Values used in `results/cp_sweep` |
|---|---|
| Eviction | `lru`, `fifo`, `s3fifo`, `sieve` |
| Prefetcher | `Mithril`, `PG`, `OBL` |
| Cache size | `16mb`, `64mb`, `256mb`, `1gb` |
| Interaction window | `0`, `1`, `10`, `100`, `1000`, `10000` |

Full factorial over traces × policies × sizes × windows. Output: `results/cp_sweep/summary.csv` plus per-run logs.

### How to reproduce

Download traces (oracleGeneral):

```bash
mkdir -p cloudphysics && cd cloudphysics
curl -sL 'https://cache-datasets.s3.amazonaws.com/?prefix=cache_dataset_oracleGeneral/2015_cloudphysics/&max-keys=1000' \
  | grep -oE 'cache_dataset_oracleGeneral/2015_cloudphysics/[^<]+\.zst' \
  | while read -r key; do wget -c "https://cache-datasets.s3.amazonaws.com/${key}"; done
cd ..
```

Run the sweep:

```bash
./scripts/run_prefetch_interaction_sweep.sh \
  --trace-dir ./cloudphysics \
  --trace-type oracleGeneral \
  --eviction lru,fifo,s3fifo,sieve \
  --prefetchers Mithril,PG,OBL \
  --sizes 16mb,64mb,256mb,1gb \
  --windows 0,1,10,100,1000,10000 \
  --jobs 4 \
  --outdir ./results/cp_sweep
```

Generate plots:

```bash
./scripts/plot_prefetch_interactions.sh
# or a subset:
./scripts/plot_prefetch_interactions.sh --traces w35,w02,w54
```

Plot layout:

```
results/cp_sweep/plots/
  vs_window/<trace>/cache_<size>.png          # multi-panel (includes useful/useless when present)
  vs_cache_size/<trace>/window_<W>.png
  useful/vs_window/<trace>/cache_<size>.png   # dedicated useful plots
  useful/vs_cache_size/<trace>/window_<W>.png
```

Combined figures include panels for prefetch→evict, total evict→prefetch, and (when `summary.csv` has the new columns) evict→useful / evict→useless. Dedicated useful plots also show useful as a share of evict→prefetch events.

## Results

### Combo ranking (joint interaction)

Combos ranked by \(\sqrt{\text{mean pf→evict%} \times \text{mean ev→pf%}}\) over runs with `window > 0` and ≥100 prefetches:

| Rank | Combo | Mean pf→evict % | Mean ev→pf % | Joint | #1 on a trace |
|---:|---|---:|---:|---:|---:|
| 1 | **sieve+Mithril** | 12.8 | 2.05 | **5.14** | **78%** |
| 2 | fifo+Mithril | 4.9 | 2.17 | 3.27 | 5% |
| 3 | lru+Mithril | 5.1 | 1.96 | 3.16 | 0% |
| 4 | sieve+PG | **17.5** | 0.06 | 1.00 | 9% |
| 5 | fifo+PG | 4.5 | 0.13 | 0.77 | 8% |
| 6 | lru+PG | 5.2 | 0.09 | 0.69 | 0% |
| 7 | sieve+OBL | 3.8 | ≈0 | 0.07 | 0% |
| 8 | fifo+OBL | 1.8 | ≈0 | 0.05 | 0% |
| 9 | lru+OBL | 1.8 | ≈0 | 0.05 | 0% |

**Takeaways:**

- **Mithril** is the only prefetcher that produces meaningful **evict→prefetch** rates.
- **PG** often shows high **prefetch→evict** (unused prefetches) but almost no re-prefetch after eviction.
- **OBL** is low on both metrics in this setup.
- **Sieve** amplifies unused-prefetch eviction versus LRU/FIFO.
- The consistently highest joint interaction combo is **`sieve + Mithril`**.

At the largest window (`10000`), sieve+Mithril averages roughly **43%** prefetch→evict and **7.6%** evict→prefetch across runs.

### Traces with high absolute interaction volume

Top traces by max (`n_prefetch_then_evict + n_evict_then_prefetch`) in any run:

| Trace | Max interactions | Typical peak config |
|---|---:|---|
| w02 | 146M | sieve+Mithril |
| w11 | 42M | sieve+Mithril |
| w19 | 33M | sieve+Mithril |
| w06 | 31M | sieve+Mithril |
| w35 | 29M | lru/sieve+Mithril |

Large absolute counts often track traces that also issue many Mithril prefetches (e.g. w02).

### Traces high on *both* percentage metrics

Traces that appear in the top-20 of **both** max percentage lists:

| Trace | Max pf→evict % | Max ev→pf % |
|---|---:|---:|
| **w35** | 94.3 | **67.2** |
| w102 | 93.1 | 34.1 |
| w19 | 97.1 | 20.5 |

**w35** is the clearest dual high-rate trace. A strong config there is **sieve + Mithril, 64mb, window=10000** (≈87% pf→evict, ≈57% ev→pf on that run).

Other high joint-percentage runners-up: **w70**, **w57**, **w102**, **w106**.

### Prefetch→evict % leaders (rate only)

Very high unused-prefetch fractions appear on several smaller / mid traces with **sieve+PG** at large windows (e.g. w54, w97, w98 often >97% pf→evict). These are “wasteful prefetch” cases, not dual-interaction cases.

### Effect of window and cache size

Qualitative patterns from the plots:

- Larger **interaction windows** increase both percentages (more future/history is eligible to count). Growth is often steep from 100 → 1000 → 10000 for Mithril.
- **Cache size** effects are combo- and trace-dependent: smaller caches tend to raise prefetch→evict (prefetches die faster); Mithril’s evict→prefetch can remain high when association-driven re-fetch continues under pressure.
- See `results/cp_sweep/plots/vs_window/` and `.../vs_cache_size/` for per-trace curves.

## Interpretation

High interaction does **not** by itself mean “bad caching,” but it indicates:

1. Prefetch traffic is consuming capacity that demand traffic then needs (**pollution**), and/or
2. The same objects are cycling through eviction and prefetch (**churn**).

For algorithm work, these metrics are useful when:

- Comparing prefetchers that look similar on miss ratio
- Tuning prefetch aggressiveness / admission
- Deciding whether a separate prefetch buffer would help (today’s code path always inserts into the main cache)

## Artifacts

| Path | Contents |
|---|---|
| `results/cp_sweep/summary.csv` | One row per run with miss ratios + interaction counters |
| `results/cp_sweep/logs/` | Per-run `cachesim` logs |
| `results/cp_sweep/plots/` | Line plots of interaction percentages |
| `cloudphysics/request_counts.csv` | Per-trace request counts |
| `scripts/run_prefetch_interaction_sweep.sh` | Sweep driver |
| `scripts/plot_prefetch_interactions.py` | Plotting script |
| `libCacheSim/cache/prefetch/prefetchInteraction.c` | Tracker implementation |

## Limitations

- Interaction windows treat “immediate” as a fixed request distance; wall-clock immediacy is not modeled.
- Percentages with very few prefetches are noisy; analyses above typically filter `n_prefetch ≥ 100` (or ≥1000 for rate leaderboards).
- `s3fifo` appears in the sweep CSV but was excluded from some aggregate combo tables when focusing on the common LRU/FIFO/sieve × prefetcher grid used for ranking; re-run ranking including `s3fifo` if needed.
- OBL is aimed at sequential block access; many CloudPhysics volumes may not stress it the same way as Mithril/PG.
- Results are for this CloudPhysics oracleGeneral release and the listed size/window grid; other datasets may differ.
