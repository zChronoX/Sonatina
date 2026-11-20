#include "abr.h"

static uint8_t sont_clamp_bp(uint32_t val, uint8_t min_bp, uint8_t max_bp)
{
    if (val < (uint32_t)min_bp) {
        return min_bp;
    }
    if (val > (uint32_t)max_bp) {
        return max_bp;
    }
    return (uint8_t)val;
}

void SontAbrInit(SONT_ABR *abr, uint8_t min_bitpool, uint8_t max_bitpool)
{
    if (abr == NULL) {
        return;
    }
    SONT_MEMSET(abr, 0, sizeof(*abr));
    if (min_bitpool < 2u) {
        min_bitpool = 2u;
    }
    if (max_bitpool < min_bitpool) {
        max_bitpool = min_bitpool;
    }
    abr->min_bitpool = min_bitpool;
    abr->max_bitpool = max_bitpool;
    abr->current_bitpool = max_bitpool;
    abr->tx_high_us = SONT_ABR_DEFAULT_HIGH_US;
    abr->tx_critical_us = SONT_ABR_DEFAULT_CRITICAL_US;
    abr->tx_low_us = SONT_ABR_DEFAULT_LOW_US;
}

void SontAbrSetThresholds(SONT_ABR *abr, uint32_t low_us, uint32_t high_us, uint32_t critical_us)
{
    if (abr == NULL || low_us >= high_us || high_us >= critical_us) {
        return;
    }
    abr->tx_low_us = low_us;
    abr->tx_high_us = high_us;
    abr->tx_critical_us = critical_us;
}

int SontAbrOnPacketSent(SONT_ABR *abr,
                        uint32_t tx_time_us,
                        bool send_ok,
                        uint8_t *new_bitpool_out)
{
    uint8_t prev_bp;
    uint8_t next_bp;
    int is_critical;
    int is_high;

    if (abr == NULL) {
        return 0;
    }
    prev_bp = abr->current_bitpool;
    next_bp = prev_bp;

    abr->tx_last_us = tx_time_us;
    if (tx_time_us > abr->tx_max_us) {
        abr->tx_max_us = tx_time_us;
    }
    if (abr->tx_avg_us == 0u) {
        abr->tx_avg_us = tx_time_us;
    } else {
        /* EWMA alpha = 1/8 */
        abr->tx_avg_us = (abr->tx_avg_us * 7u + tx_time_us) / 8u;
    }

    if (abr->cooldown_remaining > 0u) {
        abr->cooldown_remaining--;
    }

    is_critical = (!send_ok || tx_time_us >= abr->tx_critical_us);
    is_high = (is_critical || tx_time_us >= abr->tx_high_us);

    if (is_critical) {
        abr->tx_critical_count++;
    }
    if (is_high) {
        abr->tx_high_count++;
        abr->high_streak++;
        abr->window_high_packets++;
        abr->clean_windows = 0u;
    } else {
        abr->high_streak = 0u;
    }
    abr->window_packets++;

    /* Critical spike reacts aggressively (even during short cooldown). */
    if (is_critical) {
        uint32_t drop = (prev_bp > abr->min_bitpool + 8u)
                            ? 8u
                            : (uint32_t)(prev_bp - abr->min_bitpool);
        next_bp = sont_clamp_bp((uint32_t)prev_bp - drop,
                                abr->min_bitpool, abr->max_bitpool);
        abr->high_streak = 0u;
    } else if (abr->high_streak >= SONT_ABR_SHORT_STREAK_LIMIT &&
               abr->cooldown_remaining == 0u) {
        uint32_t drop = (prev_bp > abr->min_bitpool + 4u)
                            ? 4u
                            : (uint32_t)(prev_bp - abr->min_bitpool);
        next_bp = sont_clamp_bp((uint32_t)prev_bp - drop,
                                abr->min_bitpool, abr->max_bitpool);
        abr->high_streak = 0u;
    }

    /* Long-term window evaluation (step down on sustained high % or recover up). */
    if (abr->window_packets >= SONT_ABR_LONG_WINDOW_PACKETS) {
        uint32_t pct = (abr->window_high_packets * 100u) / abr->window_packets;
        if (pct >= SONT_ABR_LONG_HIGH_PCT && next_bp == prev_bp &&
            abr->cooldown_remaining == 0u) {
            uint32_t drop = (prev_bp > abr->min_bitpool + 4u)
                                ? 4u
                                : (uint32_t)(prev_bp - abr->min_bitpool);
            next_bp = sont_clamp_bp((uint32_t)prev_bp - drop,
                                    abr->min_bitpool, abr->max_bitpool);
            abr->clean_windows = 0u;
        } else if (abr->window_high_packets == 0u &&
                   abr->tx_avg_us <= abr->tx_low_us) {
            abr->clean_windows++;
            if (abr->clean_windows >= SONT_ABR_RECOVERY_WINDOWS &&
                next_bp == prev_bp &&
                abr->cooldown_remaining == 0u &&
                prev_bp < abr->max_bitpool) {
                next_bp = sont_clamp_bp((uint32_t)prev_bp + 2u,
                                        abr->min_bitpool, abr->max_bitpool);
                abr->clean_windows = 0u;
            }
        } else {
            abr->clean_windows = 0u;
        }
        abr->window_packets = 0u;
        abr->window_high_packets = 0u;
    }

    if (next_bp != prev_bp) {
        abr->current_bitpool = next_bp;
        abr->abr_adjustments++;
        abr->cooldown_remaining = SONT_ABR_COOLDOWN_PACKETS;
        if (new_bitpool_out != NULL) {
            *new_bitpool_out = next_bp;
        }
        return 1;
    }
    return 0;
}
