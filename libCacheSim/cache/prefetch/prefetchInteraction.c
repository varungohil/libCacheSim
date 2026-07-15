//
// Prefetch ↔ eviction interaction tracker.
//

#include "libCacheSim/prefetchInteraction.h"

#include <stdlib.h>
#include <string.h>
#include <strings.h>

#ifdef __cplusplus
extern "C" {
#endif

bool prefetch_interaction_is_param_key(const char *key) {
  return strcasecmp(key, "interaction-window") == 0 ||
         strcasecmp(key, "prefetch-evict-window") == 0 ||
         strcasecmp(key, "evict-prefetch-window") == 0;
}

static void parse_windows_from_params(const char *params,
                                      int64_t *prefetch_evict_window,
                                      int64_t *evict_prefetch_window) {
  *prefetch_evict_window = 0;
  *evict_prefetch_window = 0;
  if (params == NULL || params[0] == '\0') {
    return;
  }

  char *params_str = strdup(params);
  char *cursor = params_str;
  while (cursor != NULL && cursor[0] != '\0') {
    char *key = strsep(&cursor, "=");
    char *value = strsep(&cursor, ",");
    while (cursor != NULL && *cursor == ' ') {
      cursor++;
    }
    if (key == NULL || value == NULL) {
      continue;
    }
    if (strcasecmp(key, "interaction-window") == 0) {
      int64_t w = (int64_t)atoll(value);
      *prefetch_evict_window = w;
      *evict_prefetch_window = w;
    } else if (strcasecmp(key, "prefetch-evict-window") == 0) {
      *prefetch_evict_window = (int64_t)atoll(value);
    } else if (strcasecmp(key, "evict-prefetch-window") == 0) {
      *evict_prefetch_window = (int64_t)atoll(value);
    }
  }
  free(params_str);
}

static void free_pending(gpointer data) { free(data); }

static void free_event_queue(gpointer data) {
  GQueue *q = (GQueue *)data;
  if (q == NULL) {
    return;
  }
  while (!g_queue_is_empty(q)) {
    free(g_queue_pop_head(q));
  }
  g_queue_free(q);
}

static pit_pending_t *new_pending(int64_t vtime, bool from_evict_reprefetch) {
  pit_pending_t *p = (pit_pending_t *)malloc(sizeof(pit_pending_t));
  p->vtime = vtime;
  p->from_evict_reprefetch = from_evict_reprefetch;
  return p;
}

static void mark_evict_reprefetch_useless(prefetch_interaction_tracker_t *t,
                                          pit_pending_t *pending) {
  if (pending != NULL && pending->from_evict_reprefetch) {
    t->n_evict_then_useless_prefetch++;
    pending->from_evict_reprefetch = false;
  }
}

/** Count still-pending evict→prefetch objects as unused (useless). */
static void finalize_pending_evict_reprefetch(
    prefetch_interaction_tracker_t *t) {
  if (t == NULL || t->pending_prefetches == NULL) {
    return;
  }
  GHashTableIter iter;
  gpointer key, value;
  g_hash_table_iter_init(&iter, t->pending_prefetches);
  while (g_hash_table_iter_next(&iter, &key, &value)) {
    mark_evict_reprefetch_useless(t, (pit_pending_t *)value);
  }
}

static int miss_dist_bin(int64_t distance) {
  if (distance <= 1) {
    return 0;
  }
  /* floor(log2(distance)): bin i covers [2^i, 2^{i+1}). */
  int bin = 0;
  uint64_t d = (uint64_t)distance;
  while (d > 1 && bin < PIT_MISS_DIST_NBINS - 1) {
    d >>= 1;
    bin++;
  }
  return bin;
}

static void record_miss_distance(prefetch_interaction_tracker_t *t,
                                 int64_t distance) {
  if (distance < 0) {
    distance = 0;
  }
  t->miss_dist_hist[miss_dist_bin(distance)]++;
}

static void watch_pf_evict_outcome(prefetch_interaction_tracker_t *t,
                                   obj_id_t obj_id, int64_t evict_vtime) {
  if (t->awaiting_pf_evict_outcome == NULL) {
    return;
  }
  gpointer key = GSIZE_TO_POINTER(obj_id);
  GQueue *q = (GQueue *)g_hash_table_lookup(t->awaiting_pf_evict_outcome, key);
  if (q == NULL) {
    q = g_queue_new();
    g_hash_table_insert(t->awaiting_pf_evict_outcome, key, q);
  }
  pit_event_t *e = (pit_event_t *)malloc(sizeof(pit_event_t));
  e->obj_id = obj_id;
  e->vtime = evict_vtime;
  g_queue_push_tail(q, e);
}

/**
 * Resolve all queued unused-prefetch-eviction watches for obj.
 * kind: 0 = miss (+hist), 1 = no demand, 2 = reinserted then hit.
 */
static void resolve_pf_evict_watches(prefetch_interaction_tracker_t *t,
                                     obj_id_t obj_id, int64_t now, int kind) {
  if (t->awaiting_pf_evict_outcome == NULL) {
    return;
  }
  gpointer key = GSIZE_TO_POINTER(obj_id);
  GQueue *q = (GQueue *)g_hash_table_lookup(t->awaiting_pf_evict_outcome, key);
  if (q == NULL) {
    return;
  }
  while (!g_queue_is_empty(q)) {
    pit_event_t *e = (pit_event_t *)g_queue_pop_head(q);
    if (kind == 0) {
      t->n_prefetch_then_evict_then_miss++;
      record_miss_distance(t, now - e->vtime);
    } else if (kind == 1) {
      t->n_prefetch_then_evict_no_demand++;
    } else {
      t->n_prefetch_then_evict_then_hit++;
    }
    free(e);
  }
  g_hash_table_remove(t->awaiting_pf_evict_outcome, key);
}

static void finalize_pf_evict_outcomes(prefetch_interaction_tracker_t *t) {
  if (t == NULL || t->awaiting_pf_evict_outcome == NULL) {
    return;
  }
  GList *keys = g_hash_table_get_keys(t->awaiting_pf_evict_outcome);
  for (GList *node = keys; node != NULL; node = node->next) {
    obj_id_t obj_id = (obj_id_t)GPOINTER_TO_SIZE(node->data);
    resolve_pf_evict_watches(t, obj_id, 0, /*no demand*/ 1);
  }
  g_list_free(keys);
}

prefetch_interaction_tracker_t *prefetch_interaction_create(
    int64_t prefetch_evict_window, int64_t evict_prefetch_window) {
  if (prefetch_evict_window <= 0 && evict_prefetch_window <= 0) {
    return NULL;
  }

  prefetch_interaction_tracker_t *t = (prefetch_interaction_tracker_t *)malloc(
      sizeof(prefetch_interaction_tracker_t));
  memset(t, 0, sizeof(*t));
  t->prefetch_evict_window =
      prefetch_evict_window > 0 ? prefetch_evict_window : 0;
  t->evict_prefetch_window =
      evict_prefetch_window > 0 ? evict_prefetch_window : 0;

  /* Always track pending prefetches when either window is on so we can
   * classify evict→prefetch useful vs useless via demand hits / unused
   * eviction. */
  t->pending_prefetches =
      g_hash_table_new_full(g_direct_hash, g_direct_equal, NULL, free_pending);

  t->awaiting_pf_evict_outcome = g_hash_table_new_full(
      g_direct_hash, g_direct_equal, NULL, free_event_queue);

  if (t->evict_prefetch_window > 0) {
    t->recent_evictions =
        g_hash_table_new_full(g_direct_hash, g_direct_equal, NULL, NULL);
    t->eviction_order = g_queue_new();
  }
  return t;
}

prefetch_interaction_tracker_t *prefetch_interaction_create_from_params(
    const char *params) {
  int64_t prefetch_evict_window = 0;
  int64_t evict_prefetch_window = 0;
  parse_windows_from_params(params, &prefetch_evict_window,
                            &evict_prefetch_window);
  return prefetch_interaction_create(prefetch_evict_window,
                                     evict_prefetch_window);
}

prefetch_interaction_tracker_t *prefetch_interaction_clone(
    const prefetch_interaction_tracker_t *src) {
  if (src == NULL) {
    return NULL;
  }
  return prefetch_interaction_create(src->prefetch_evict_window,
                                     src->evict_prefetch_window);
}

void prefetch_interaction_free(prefetch_interaction_tracker_t *t) {
  if (t == NULL) {
    return;
  }
  if (t->pending_prefetches) {
    g_hash_table_destroy(t->pending_prefetches);
  }
  if (t->awaiting_pf_evict_outcome) {
    g_hash_table_destroy(t->awaiting_pf_evict_outcome);
  }
  if (t->recent_evictions) {
    g_hash_table_destroy(t->recent_evictions);
  }
  if (t->eviction_order) {
    while (!g_queue_is_empty(t->eviction_order)) {
      pit_event_t *e = (pit_event_t *)g_queue_pop_head(t->eviction_order);
      free(e);
    }
    g_queue_free(t->eviction_order);
  }
  free(t);
}

static void prune_recent_evictions(prefetch_interaction_tracker_t *t,
                                   int64_t now) {
  if (t->eviction_order == NULL || t->evict_prefetch_window <= 0) {
    return;
  }
  while (!g_queue_is_empty(t->eviction_order)) {
    pit_event_t *e = (pit_event_t *)g_queue_peek_head(t->eviction_order);
    if (now - e->vtime <= t->evict_prefetch_window) {
      break;
    }
    g_queue_pop_head(t->eviction_order);
    gpointer p =
        g_hash_table_lookup(t->recent_evictions, GSIZE_TO_POINTER(e->obj_id));
    if (p != NULL && (int64_t)GPOINTER_TO_SIZE(p) == e->vtime) {
      g_hash_table_remove(t->recent_evictions, GSIZE_TO_POINTER(e->obj_id));
    }
    free(e);
  }
}

static void prune_pending_prefetches(prefetch_interaction_tracker_t *t,
                                     int64_t now) {
  if (t->pending_prefetches == NULL || t->prefetch_evict_window <= 0) {
    return;
  }
  /* Stale pending entries are past the prefetch→evict window. If they came
   * from evict→prefetch and never got a demand hit, count them useless. */
  GList *stale = NULL;
  GHashTableIter iter;
  gpointer key, value;
  g_hash_table_iter_init(&iter, t->pending_prefetches);
  while (g_hash_table_iter_next(&iter, &key, &value)) {
    pit_pending_t *pending = (pit_pending_t *)value;
    if (now - pending->vtime > t->prefetch_evict_window) {
      mark_evict_reprefetch_useless(t, pending);
      stale = g_list_prepend(stale, key);
    }
  }
  for (GList *node = stale; node != NULL; node = node->next) {
    g_hash_table_remove(t->pending_prefetches, node->data);
  }
  g_list_free(stale);
}

void prefetch_interaction_on_prefetch(prefetch_interaction_tracker_t *t,
                                      obj_id_t obj_id, int64_t vtime) {
  if (t == NULL) {
    return;
  }
  t->n_prefetch++;

  bool from_evict_reprefetch = false;
  if (t->evict_prefetch_window > 0 && t->recent_evictions != NULL) {
    prune_recent_evictions(t, vtime);
    gpointer p =
        g_hash_table_lookup(t->recent_evictions, GSIZE_TO_POINTER(obj_id));
    if (p != NULL) {
      int64_t evict_vt = (int64_t)GPOINTER_TO_SIZE(p);
      if (vtime - evict_vt <= t->evict_prefetch_window) {
        t->n_evict_then_prefetch++;
        from_evict_reprefetch = true;
      }
      g_hash_table_remove(t->recent_evictions, GSIZE_TO_POINTER(obj_id));
    }
  }

  if (t->pending_prefetches != NULL) {
    if (t->prefetch_evict_window > 0) {
      prune_pending_prefetches(t, vtime);
    }
    /* Replace any previous pending watch for this object. If it was an
     * unresolved evict→reprefetch, count it useless before overwriting. */
    pit_pending_t *old = (pit_pending_t *)g_hash_table_lookup(
        t->pending_prefetches, GSIZE_TO_POINTER(obj_id));
    if (old != NULL) {
      mark_evict_reprefetch_useless(t, old);
    }
    g_hash_table_insert(t->pending_prefetches, GSIZE_TO_POINTER(obj_id),
                        new_pending(vtime, from_evict_reprefetch));
  }
}

void prefetch_interaction_on_demand_hit(prefetch_interaction_tracker_t *t,
                                        obj_id_t obj_id) {
  if (t == NULL) {
    return;
  }
  if (t->pending_prefetches != NULL) {
    pit_pending_t *pending = (pit_pending_t *)g_hash_table_lookup(
        t->pending_prefetches, GSIZE_TO_POINTER(obj_id));
    if (pending != NULL) {
      if (pending->from_evict_reprefetch) {
        t->n_evict_then_useful_prefetch++;
        pending->from_evict_reprefetch = false;
      }
      g_hash_table_remove(t->pending_prefetches, GSIZE_TO_POINTER(obj_id));
    }
  }
  /* Object is in cache → prior unused pf→evicts were followed by reinsert
   * (prefetch or otherwise) without a demand miss. */
  resolve_pf_evict_watches(t, obj_id, 0, /*reinsert hit*/ 2);
}

void prefetch_interaction_on_demand_miss(prefetch_interaction_tracker_t *t,
                                         obj_id_t obj_id, int64_t vtime) {
  if (t == NULL) {
    return;
  }
  resolve_pf_evict_watches(t, obj_id, vtime, /*miss*/ 0);
}

void prefetch_interaction_on_evict(prefetch_interaction_tracker_t *t,
                                   obj_id_t obj_id, int64_t vtime) {
  if (t == NULL) {
    return;
  }
  t->n_evict++;

  if (t->pending_prefetches != NULL) {
    pit_pending_t *pending = (pit_pending_t *)g_hash_table_lookup(
        t->pending_prefetches, GSIZE_TO_POINTER(obj_id));
    if (pending != NULL) {
      bool count_pf_evict =
          t->prefetch_evict_window > 0 &&
          (vtime - pending->vtime <= t->prefetch_evict_window);
      if (count_pf_evict) {
        t->n_prefetch_then_evict++;
        watch_pf_evict_outcome(t, obj_id, vtime);
      }
      /* Unused eviction of an evict→reprefetch counts as useless whether or
       * not it is still inside W_future. */
      mark_evict_reprefetch_useless(t, pending);
      g_hash_table_remove(t->pending_prefetches, GSIZE_TO_POINTER(obj_id));
    }
  }

  if (t->evict_prefetch_window > 0 && t->recent_evictions != NULL) {
    prune_recent_evictions(t, vtime);
    g_hash_table_insert(t->recent_evictions, GSIZE_TO_POINTER(obj_id),
                        GSIZE_TO_POINTER((gsize)vtime));
    pit_event_t *e = (pit_event_t *)malloc(sizeof(pit_event_t));
    e->obj_id = obj_id;
    e->vtime = vtime;
    g_queue_push_tail(t->eviction_order, e);
  }
}

void prefetch_interaction_print(prefetch_interaction_tracker_t *t, FILE *out) {
  if (t == NULL || out == NULL) {
    return;
  }
  finalize_pending_evict_reprefetch(t);
  finalize_pf_evict_outcomes(t);

  double pf_ratio =
      t->n_prefetch > 0
          ? 100.0 * (double)t->n_prefetch_then_evict / (double)t->n_prefetch
          : 0.0;
  double pf_miss_of =
      t->n_prefetch_then_evict > 0
          ? 100.0 * (double)t->n_prefetch_then_evict_then_miss /
                (double)t->n_prefetch_then_evict
          : 0.0;
  double pf_nodemand_of =
      t->n_prefetch_then_evict > 0
          ? 100.0 * (double)t->n_prefetch_then_evict_no_demand /
                (double)t->n_prefetch_then_evict
          : 0.0;
  double pf_hit_of =
      t->n_prefetch_then_evict > 0
          ? 100.0 * (double)t->n_prefetch_then_evict_then_hit /
                (double)t->n_prefetch_then_evict
          : 0.0;
  double ev_ratio =
      t->n_evict > 0
          ? 100.0 * (double)t->n_evict_then_prefetch / (double)t->n_evict
          : 0.0;
  double useful_of_ep =
      t->n_evict_then_prefetch > 0
          ? 100.0 * (double)t->n_evict_then_useful_prefetch /
                (double)t->n_evict_then_prefetch
          : 0.0;
  double useless_of_ep =
      t->n_evict_then_prefetch > 0
          ? 100.0 * (double)t->n_evict_then_useless_prefetch /
                (double)t->n_evict_then_prefetch
          : 0.0;

  fprintf(out,
          "prefetch interaction (W_future=%lld, W_history=%lld):\n"
          "  prefetches:               %llu\n"
          "  prefetch→evict (unused):  %llu  (%.1f%%)\n"
          "    then miss:              %llu  (%.1f%% of prefetch→evict)\n"
          "    no later demand:        %llu  (%.1f%% of prefetch→evict)\n"
          "    reinserted then hit:    %llu  (%.1f%% of prefetch→evict)\n"
          "  evictions:                %llu\n"
          "  evict→prefetch:           %llu  (%.1f%% of evictions)\n"
          "    useful (demand hit):    %llu  (%.1f%% of evict→prefetch)\n"
          "    useless (never used):   %llu  (%.1f%% of evict→prefetch)\n",
          (long long)t->prefetch_evict_window,
          (long long)t->evict_prefetch_window,
          (unsigned long long)t->n_prefetch,
          (unsigned long long)t->n_prefetch_then_evict, pf_ratio,
          (unsigned long long)t->n_prefetch_then_evict_then_miss, pf_miss_of,
          (unsigned long long)t->n_prefetch_then_evict_no_demand, pf_nodemand_of,
          (unsigned long long)t->n_prefetch_then_evict_then_hit, pf_hit_of,
          (unsigned long long)t->n_evict,
          (unsigned long long)t->n_evict_then_prefetch, ev_ratio,
          (unsigned long long)t->n_evict_then_useful_prefetch, useful_of_ep,
          (unsigned long long)t->n_evict_then_useless_prefetch, useless_of_ep);

  fprintf(out,
          "  prefetch→evict→miss distance hist (log2 bins, requests):\n");
  int any = 0;
  for (int i = 0; i < PIT_MISS_DIST_NBINS; i++) {
    if (t->miss_dist_hist[i] == 0) {
      continue;
    }
    any = 1;
    unsigned long long lo = 1ULL << i;
    if (i + 1 < PIT_MISS_DIST_NBINS) {
      unsigned long long hi = 1ULL << (i + 1);
      fprintf(out, "    [%llu,%llu): %llu\n", lo, hi,
              (unsigned long long)t->miss_dist_hist[i]);
    } else {
      fprintf(out, "    [%llu,+inf): %llu\n", lo,
              (unsigned long long)t->miss_dist_hist[i]);
    }
  }
  if (!any) {
    fprintf(out, "    (empty)\n");
  }
}

#ifdef __cplusplus
}
#endif
