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

prefetch_interaction_tracker_t *prefetch_interaction_create(
    int64_t prefetch_evict_window, int64_t evict_prefetch_window) {
  if (prefetch_evict_window <= 0 && evict_prefetch_window <= 0) {
    return NULL;
  }

  prefetch_interaction_tracker_t *t =
      (prefetch_interaction_tracker_t *)malloc(
          sizeof(prefetch_interaction_tracker_t));
  memset(t, 0, sizeof(*t));
  t->prefetch_evict_window =
      prefetch_evict_window > 0 ? prefetch_evict_window : 0;
  t->evict_prefetch_window =
      evict_prefetch_window > 0 ? evict_prefetch_window : 0;

  if (t->prefetch_evict_window > 0) {
    t->pending_prefetches =
        g_hash_table_new_full(g_direct_hash, g_direct_equal, NULL, NULL);
  }
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
  /* Collect stale keys then remove (cannot mutate while iterating). */
  GList *stale = NULL;
  GHashTableIter iter;
  gpointer key, value;
  g_hash_table_iter_init(&iter, t->pending_prefetches);
  while (g_hash_table_iter_next(&iter, &key, &value)) {
    int64_t pf_vt = (int64_t)GPOINTER_TO_SIZE(value);
    if (now - pf_vt > t->prefetch_evict_window) {
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

  if (t->evict_prefetch_window > 0 && t->recent_evictions != NULL) {
    prune_recent_evictions(t, vtime);
    gpointer p =
        g_hash_table_lookup(t->recent_evictions, GSIZE_TO_POINTER(obj_id));
    if (p != NULL) {
      int64_t evict_vt = (int64_t)GPOINTER_TO_SIZE(p);
      if (vtime - evict_vt <= t->evict_prefetch_window) {
        t->n_evict_then_prefetch++;
      }
      g_hash_table_remove(t->recent_evictions, GSIZE_TO_POINTER(obj_id));
    }
  }

  if (t->prefetch_evict_window > 0 && t->pending_prefetches != NULL) {
    prune_pending_prefetches(t, vtime);
    g_hash_table_insert(t->pending_prefetches, GSIZE_TO_POINTER(obj_id),
                        GSIZE_TO_POINTER((gsize)vtime));
  }
}

void prefetch_interaction_on_demand_hit(prefetch_interaction_tracker_t *t,
                                        obj_id_t obj_id) {
  if (t == NULL || t->pending_prefetches == NULL) {
    return;
  }
  g_hash_table_remove(t->pending_prefetches, GSIZE_TO_POINTER(obj_id));
}

void prefetch_interaction_on_evict(prefetch_interaction_tracker_t *t,
                                   obj_id_t obj_id, int64_t vtime) {
  if (t == NULL) {
    return;
  }
  t->n_evict++;

  if (t->prefetch_evict_window > 0 && t->pending_prefetches != NULL) {
    gpointer p =
        g_hash_table_lookup(t->pending_prefetches, GSIZE_TO_POINTER(obj_id));
    if (p != NULL) {
      int64_t pf_vt = (int64_t)GPOINTER_TO_SIZE(p);
      if (vtime - pf_vt <= t->prefetch_evict_window) {
        t->n_prefetch_then_evict++;
      }
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

void prefetch_interaction_print(const prefetch_interaction_tracker_t *t,
                                FILE *out) {
  if (t == NULL || out == NULL) {
    return;
  }
  double pf_ratio =
      t->n_prefetch > 0
          ? 100.0 * (double)t->n_prefetch_then_evict / (double)t->n_prefetch
          : 0.0;
  double ev_ratio =
      t->n_evict > 0
          ? 100.0 * (double)t->n_evict_then_prefetch / (double)t->n_evict
          : 0.0;
  fprintf(out,
          "prefetch interaction (W_future=%lld, W_history=%lld):\n"
          "  prefetches:               %llu\n"
          "  prefetch→evict (unused):  %llu  (%.1f%%)\n"
          "  evictions:                %llu\n"
          "  evict→prefetch:           %llu  (%.1f%%)\n",
          (long long)t->prefetch_evict_window,
          (long long)t->evict_prefetch_window,
          (unsigned long long)t->n_prefetch,
          (unsigned long long)t->n_prefetch_then_evict, pf_ratio,
          (unsigned long long)t->n_evict,
          (unsigned long long)t->n_evict_then_prefetch, ev_ratio);
}

#ifdef __cplusplus
}
#endif
