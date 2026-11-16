/* Portable fixed-capacity trace event ring for AVDTP/RTP/ABR telemetry (A4). */
#ifndef SONATINA_TRACE_RING_H
#define SONATINA_TRACE_RING_H

#include "portable.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SONT_TRACE_RING_CAPACITY 32u

typedef struct _SONT_TRACE_ENTRY {
    uint64_t timestamp_us;
    uint32_t kind;
    uint32_t arg0;
    uint32_t arg1;
} SONT_TRACE_ENTRY;

typedef struct _SONT_TRACE_RING {
    uint32_t total_events;
    uint32_t count;
    uint32_t write_idx;
    SONT_TRACE_ENTRY entries[SONT_TRACE_RING_CAPACITY];
} SONT_TRACE_RING;

void SontTraceRingInit(SONT_TRACE_RING *ring);
void SontTraceRingPush(SONT_TRACE_RING *ring,
                       uint64_t timestamp_us,
                       uint32_t kind,
                       uint32_t arg0,
                       uint32_t arg1);
/* Copy up to `cap` entries in oldest-to-newest order, returning count copied. */
uint32_t SontTraceRingSnapshot(const SONT_TRACE_RING *ring,
                               SONT_TRACE_ENTRY *out,
                               uint32_t cap);

#ifdef __cplusplus
}
#endif

#endif /* SONATINA_TRACE_RING_H */
