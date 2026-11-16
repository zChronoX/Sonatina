/* Portable debounce + cooldown policy for SonatinaSvc.
 *
 * Mirrors ApplyPolicy in sonatina/watcher.py so the C service and Python
 * reference share identical burst-coalescing and cooldown semantics.
 */
#ifndef SONATINA_APPLY_POLICY_H
#define SONATINA_APPLY_POLICY_H

#include "portable.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum _SONT_POLICY_OUTCOME {
    SONT_POLICY_NONE = 0,
    SONT_POLICY_APPLY = 1,
    SONT_POLICY_COOLDOWN = 2
} SONT_POLICY_OUTCOME;

typedef struct _SONT_APPLY_POLICY {
    uint64_t debounce_ms;
    uint64_t cooldown_ms;
    int pending;
    uint64_t due_ms;
    int has_last_apply;
    uint64_t last_apply_ms;
} SONT_APPLY_POLICY;

void SontPolicyInit(SONT_APPLY_POLICY *policy, uint64_t debounce_ms, uint64_t cooldown_ms);
void SontPolicyNotify(SONT_APPLY_POLICY *policy, uint64_t now_ms);
SONT_POLICY_OUTCOME SontPolicyPoll(SONT_APPLY_POLICY *policy, uint64_t now_ms);
int SontPolicyElapsedSinceApplyMs(const SONT_APPLY_POLICY *policy,
                                  uint64_t now_ms,
                                  uint64_t *elapsed_ms);

#ifdef __cplusplus
}
#endif

#endif /* SONATINA_APPLY_POLICY_H */
