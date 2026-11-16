/* Portable Adaptive Bitrate (ABR) and tx-latency tracker (D6).
 *
 * Monitors per-packet ACL send latency against short-term and long-term
 * thresholds, steps SBC bitpool down on congestion and recovers gradually when
 * the radio link stays clean.
 */
#ifndef SONATINA_ABR_H
#define SONATINA_ABR_H

#include "portable.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SONT_ABR_DEFAULT_HIGH_US       12000u
#define SONT_ABR_DEFAULT_CRITICAL_US   25000u
#define SONT_ABR_DEFAULT_LOW_US         5000u
#define SONT_ABR_SHORT_STREAK_LIMIT        3u
#define SONT_ABR_LONG_WINDOW_PACKETS      50u
#define SONT_ABR_LONG_HIGH_PCT            25u
#define SONT_ABR_RECOVERY_WINDOWS          2u
#define SONT_ABR_COOLDOWN_PACKETS         15u

typedef struct _SONT_ABR {
    uint8_t min_bitpool;
    uint8_t max_bitpool;
    uint8_t current_bitpool;

    uint32_t tx_high_us;
    uint32_t tx_critical_us;
    uint32_t tx_low_us;

    uint32_t tx_last_us;
    uint32_t tx_avg_us;
    uint32_t tx_max_us;
    uint32_t tx_high_count;
    uint32_t tx_critical_count;
    uint32_t abr_adjustments;

    uint32_t high_streak;
    uint32_t window_packets;
    uint32_t window_high_packets;
    uint32_t clean_windows;
    uint32_t cooldown_remaining;
} SONT_ABR;

void SontAbrInit(SONT_ABR *abr, uint8_t min_bitpool, uint8_t max_bitpool);

/* Record one media packet transmission outcome.
 * Returns 1 if `current_bitpool` changed (written to *new_bitpool_out), else 0. */
int SontAbrOnPacketSent(SONT_ABR *abr,
                        uint32_t tx_time_us,
                        bool send_ok,
                        uint8_t *new_bitpool_out);

#ifdef __cplusplus
}
#endif

#endif /* SONATINA_ABR_H */
