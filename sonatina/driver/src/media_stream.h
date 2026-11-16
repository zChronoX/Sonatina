/* Portable SBC + RTP media packetizer and circular PCM staging ring.
 *
 * Compiles cleanly under both SONATINA_HOST (unit tests) and SONATINA_KERNEL.
 */
#ifndef SONATINA_MEDIA_STREAM_H
#define SONATINA_MEDIA_STREAM_H

#include "codec_sbc.h"
#include "rtp.h"

#define SONT_MEDIA_MAX_FRAME_BUF 2048u

typedef struct _SONT_PCM_RING {
    uint8_t *buf;
    size_t capacity;
    size_t read_pos;
    size_t write_pos;
    size_t used;
    uint64_t dropped_bytes;
} SONT_PCM_RING;

typedef struct _SONT_MEDIA_PACKETIZER {
    SONT_SBC_ENCODER *encoder;
    SONT_SBC_PARAMS sbc_params;
    uint8_t bitpool;
    uint16_t mtu;
    uint32_t ssrc;

    uint16_t next_seq;
    uint32_t next_timestamp;
    bool sent_first;

    unsigned channels;
    unsigned samples_per_frame;
    unsigned frame_bytes;
    uint8_t max_frames_per_packet;
    uint8_t frames_pending;

    size_t frame_buf_used;
    uint8_t frame_buf[SONT_MEDIA_MAX_FRAME_BUF];
} SONT_MEDIA_PACKETIZER;

void SontPcmRingInit(SONT_PCM_RING *ring, uint8_t *backing, size_t capacity);
void SontPcmRingReset(SONT_PCM_RING *ring);
size_t SontPcmRingAvailable(const SONT_PCM_RING *ring);
size_t SontPcmRingWrite(SONT_PCM_RING *ring, const uint8_t *data, size_t len);
size_t SontPcmRingRead(SONT_PCM_RING *ring, uint8_t *out, size_t len);

bool SontMediaInit(SONT_MEDIA_PACKETIZER *ctx,
                   const SONT_SBC_PARAMS *params,
                   uint8_t bitpool,
                   uint16_t mtu,
                   uint32_t ssrc);
bool SontMediaSetBitpool(SONT_MEDIA_PACKETIZER *ctx, uint8_t bitpool);
void SontMediaDestroy(SONT_MEDIA_PACKETIZER *ctx);

/* Encodes one SBC frame worth of interleaved int16 PCM samples and appends it
 * to the pending RTP packet.
 * Returns:
 *   1 if `rtp_out` now holds a complete RTP packet (`*rtp_len` bytes),
 *   0 if the SBC frame was buffered and more frames are needed for this MTU,
 *  -1 on error. */
int SontMediaPushPcmFrame(SONT_MEDIA_PACKETIZER *ctx,
                          const int16_t *pcm,
                          size_t samples_per_ch,
                          uint8_t *rtp_out,
                          size_t rtp_cap,
                          size_t *rtp_len);

/* Emits any buffered SBC frames immediately as an RTP packet.
 * Returns 1 if a packet was produced, 0 if nothing was buffered, -1 on error. */
int SontMediaFlush(SONT_MEDIA_PACKETIZER *ctx,
                   uint8_t *rtp_out,
                   size_t rtp_cap,
                   size_t *rtp_len);

#endif /* SONATINA_MEDIA_STREAM_H */
