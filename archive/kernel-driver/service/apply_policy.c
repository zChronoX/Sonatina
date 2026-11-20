#include "apply_policy.h"

void SontPolicyInit(SONT_APPLY_POLICY *policy, uint64_t debounce_ms, uint64_t cooldown_ms)
{
    if (policy == NULL) {
        return;
    }
    policy->debounce_ms = debounce_ms;
    policy->cooldown_ms = cooldown_ms;
    policy->pending = 0;
    policy->due_ms = 0;
    policy->has_last_apply = 0;
    policy->last_apply_ms = 0;
}

void SontPolicyNotify(SONT_APPLY_POLICY *policy, uint64_t now_ms)
{
    if (policy == NULL) {
        return;
    }
    policy->pending = 1;
    policy->due_ms = now_ms + policy->debounce_ms;
}

SONT_POLICY_OUTCOME SontPolicyPoll(SONT_APPLY_POLICY *policy, uint64_t now_ms)
{
    if (policy == NULL || !policy->pending || now_ms < policy->due_ms) {
        return SONT_POLICY_NONE;
    }
    policy->pending = 0;
    if (policy->has_last_apply &&
        (now_ms - policy->last_apply_ms) < policy->cooldown_ms) {
        return SONT_POLICY_COOLDOWN;
    }
    policy->has_last_apply = 1;
    policy->last_apply_ms = now_ms;
    return SONT_POLICY_APPLY;
}

int SontPolicyElapsedSinceApplyMs(const SONT_APPLY_POLICY *policy,
                                  uint64_t now_ms,
                                  uint64_t *elapsed_ms)
{
    if (policy == NULL || !policy->has_last_apply || elapsed_ms == NULL) {
        return 0;
    }
    *elapsed_ms = (now_ms >= policy->last_apply_ms)
                      ? (now_ms - policy->last_apply_ms)
                      : 0;
    return 1;
}
