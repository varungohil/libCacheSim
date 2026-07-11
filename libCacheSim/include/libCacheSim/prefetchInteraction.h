//
// Track prefetch ↔ eviction interactions within a configurable request window.
//
// Metrics:
//   prefetch→evict: prefetched object evicted within W_future requests
//                   before any demand hit
//   evict→prefetch: object prefetched within W_history requests of its eviction
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

typedef struct {
  int64_t prefetch_evict_window;  /* W_future; 0 disables */
  int64_t evict_prefetch_window;  /* W_history; 0 disables */

  /* obj_id -> vtime of most recent unused prefetch */
  GHashTable *pending_prefetches;
  /* obj_id -> vtime of most recent eviction */
  GHashTable *recent_evictions;
  /* ordered eviction events for pruning (pit_event_t *) */
  GQueue *eviction_order;

  uint64_t n_prefetch;
  uint64_t n_prefetch_then_evict;
  uint64_t n_evict;
  uint64_t n_evict_then_prefetch;
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

void prefetch_interaction_on_evict(prefetch_interaction_tracker_t *t,
                                   obj_id_t obj_id, int64_t vtime);

void prefetch_interaction_print(const prefetch_interaction_tracker_t *t,
                                FILE *out);

/** True if key is an interaction-tracker param (so algo parsers can ignore). */
bool prefetch_interaction_is_param_key(const char *key);

#ifdef __cplusplus
}
#endif
