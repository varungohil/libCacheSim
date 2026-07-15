//
// Track prefetch ↔ eviction interactions within a configurable request window.
//
// Metrics:
//   prefetch→evict: prefetched object evicted within W_future requests
//                   before any demand hit
//     - then miss: later demand miss for that object (premature prefetch)
//     - no later demand: object never demand-requested again
//     - reinserted then hit: object brought back (e.g. re-prefetch) and
//       demand-hit without an intervening demand miss
//   evict→prefetch: object prefetched within W_history requests of its eviction
//     - useful: that re-prefetch later received a demand hit
//     - useless: that re-prefetch was never demand-hit (evicted unused or
//                still unused at end of simulation)
//

#pragma once

#include <glib.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "../config.h"
#include "request.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Number of log2 distance histogram bins: covers [1, 2^N). */
#define PIT_MISS_DIST_NBINS 40

typedef struct {
  int64_t vtime;
  bool from_evict_reprefetch;
} pit_pending_t;

typedef struct {
  int64_t prefetch_evict_window; /* W_future; 0 disables */
  int64_t evict_prefetch_window; /* W_history; 0 disables */

  /* obj_id -> pit_pending_t* for unused prefetches */
  GHashTable *pending_prefetches;
  /* obj_id -> vtime of most recent eviction */
  GHashTable *recent_evictions;
  /* ordered eviction events for pruning (pit_event_t *) */
  GQueue *eviction_order;

  /* obj_id -> GQueue* of pit_event_t* (eviction vtimes) awaiting
   * prefetch→evict outcome classification */
  GHashTable *awaiting_pf_evict_outcome;

  uint64_t n_prefetch;
  uint64_t n_prefetch_then_evict;
  uint64_t n_prefetch_then_evict_then_miss;
  uint64_t n_prefetch_then_evict_no_demand;
  uint64_t n_prefetch_then_evict_then_hit; /* reinserted, then demand hit */
  uint64_t n_evict;
  uint64_t n_evict_then_prefetch;
  uint64_t n_evict_then_useful_prefetch;
  uint64_t n_evict_then_useless_prefetch;

  /* Log2 histogram of (miss_vtime - pf_evict_vtime) for →miss events.
   * Bin i covers distances in [2^i, 2^{i+1}). Distances >= 2^NBINS go in
   * the last bin. */
  uint64_t miss_dist_hist[PIT_MISS_DIST_NBINS];
} prefetch_interaction_tracker_t;

typedef struct {
  obj_id_t obj_id;
  int64_t vtime;
} pit_event_t;

/**
 * @brief Create a tracker. Both windows 0 => returns NULL (disabled).
 */
prefetch_interaction_tracker_t *prefetch_interaction_create(
    int64_t prefetch_evict_window, int64_t evict_prefetch_window);

/**
 * @brief Parse interaction-window / prefetch-evict-window /
 *        evict-prefetch-window from a prefetch-params string.
 *        Returns NULL if disabled or params is NULL.
 */
prefetch_interaction_tracker_t *prefetch_interaction_create_from_params(
    const char *params);

/** Clone with same windows; counters and tables start empty. */
prefetch_interaction_tracker_t *prefetch_interaction_clone(
    const prefetch_interaction_tracker_t *src);

void prefetch_interaction_free(prefetch_interaction_tracker_t *t);

void prefetch_interaction_on_prefetch(prefetch_interaction_tracker_t *t,
                                      obj_id_t obj_id, int64_t vtime);

void prefetch_interaction_on_demand_hit(prefetch_interaction_tracker_t *t,
                                        obj_id_t obj_id);

/** Demand request that missed (object not in cache). */
void prefetch_interaction_on_demand_miss(prefetch_interaction_tracker_t *t,
                                         obj_id_t obj_id, int64_t vtime);

void prefetch_interaction_on_evict(prefetch_interaction_tracker_t *t,
                                   obj_id_t obj_id, int64_t vtime);

/** Classify remaining pending outcomes, then print. */
void prefetch_interaction_print(prefetch_interaction_tracker_t *t, FILE *out);

/** True if key is an interaction-tracker param (so algo parsers can ignore). */
bool prefetch_interaction_is_param_key(const char *key);

#ifdef __cplusplus
}
#endif
