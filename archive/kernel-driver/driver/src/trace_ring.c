#include "trace_ring.h"

void SontTraceRingInit(SONT_TRACE_RING *ring)
{
    if (ring == NULL) {
        return;
    }
    SONT_MEMSET(ring, 0, sizeof(*ring));
}

void SontTraceRingPush(SONT_TRACE_RING *ring,
                       uint64_t timestamp_us,
                       uint32_t kind,
                       uint32_t arg0,
                       uint32_t arg1)
{
    SONT_TRACE_ENTRY *slot;

    if (ring == NULL) {
        return;
    }
    slot = &ring->entries[ring->write_idx % SONT_TRACE_RING_CAPACITY];
    slot->timestamp_us = timestamp_us;
    slot->kind = kind;
    slot->arg0 = arg0;
    slot->arg1 = arg1;

    ring->write_idx = (ring->write_idx + 1u) % SONT_TRACE_RING_CAPACITY;
    if (ring->count < SONT_TRACE_RING_CAPACITY) {
        ring->count++;
    }
    ring->total_events++;
}

uint32_t SontTraceRingSnapshot(const SONT_TRACE_RING *ring,
                               SONT_TRACE_ENTRY *out,
                               uint32_t cap)
{
    uint32_t to_copy;
    uint32_t start;
    uint32_t i;

    if (ring == NULL || out == NULL || cap == 0 || ring->count == 0) {
        return 0;
    }
    to_copy = (ring->count < cap) ? ring->count : cap;
    /* Oldest entry is at (write_idx - count) mod CAP; we return up to the
     * newest `to_copy` entries in chronological order. */
    start = (ring->write_idx + SONT_TRACE_RING_CAPACITY - to_copy) %
            SONT_TRACE_RING_CAPACITY;
    for (i = 0; i < to_copy; i++) {
        out[i] = ring->entries[(start + i) % SONT_TRACE_RING_CAPACITY];
    }
    return to_copy;
}
