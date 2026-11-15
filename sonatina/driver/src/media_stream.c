#include "media_stream.h"

void SontPcmRingInit(SONT_PCM_RING *ring, uint8_t *backing, size_t capacity)
{
    if (ring == NULL) {
        return;
    }
    SONT_MEMSET(ring, 0, sizeof(*ring));
    ring->buf = backing;
    ring->capacity = (backing != NULL) ? capacity : 0;
}

void SontPcmRingReset(SONT_PCM_RING *ring)
{
    if (ring == NULL) {
        return;
    }
    ring->read_pos = 0;
    ring->write_pos = 0;
    ring->used = 0;
    ring->dropped_bytes = 0;
}

size_t SontPcmRingAvailable(const SONT_PCM_RING *ring)
{
    return (ring != NULL) ? ring->used : 0;
}

size_t SontPcmRingWrite(SONT_PCM_RING *ring, const uint8_t *data, size_t len)
{
    size_t tail;
    size_t first;

    if (ring == NULL || ring->buf == NULL || ring->capacity == 0 ||
        data == NULL || len == 0) {
        return 0;
    }

    if (len >= ring->capacity) {
        size_t skip = len - ring->capacity;
        ring->dropped_bytes += (uint64_t)(ring->used + skip);
        SONT_MEMCPY(ring->buf, data + skip, ring->capacity);
        ring->read_pos = 0;
        ring->write_pos = 0;
        ring->used = ring->capacity;
        return ring->capacity;
    }

    if (ring->used + len > ring->capacity) {
        size_t overflow = (ring->used + len) - ring->capacity;
        ring->read_pos = (ring->read_pos + overflow) % ring->capacity;
        ring->used -= overflow;
        ring->dropped_bytes += (uint64_t)overflow;
    }

    tail = ring->capacity - ring->write_pos;
    first = (len < tail) ? len : tail;
    SONT_MEMCPY(ring->buf + ring->write_pos, data, first);
    if (len > first) {
        SONT_MEMCPY(ring->buf, data + first, len - first);
    }
    ring->write_pos = (ring->write_pos + len) % ring->capacity;
    ring->used += len;
    return len;
}

size_t SontPcmRingRead(SONT_PCM_RING *ring, uint8_t *out, size_t len)
{
    size_t to_read;
    size_t tail;
    size_t first;

    if (ring == NULL || ring->buf == NULL || out == NULL || len == 0 ||
        ring->used == 0) {
        return 0;
    }

    to_read = (len < ring->used) ? len : ring->used;
    tail = ring->capacity - ring->read_pos;
    first = (to_read < tail) ? to_read : tail;
    SONT_MEMCPY(out, ring->buf + ring->read_pos, first);
    if (to_read > first) {
        SONT_MEMCPY(out + first, ring->buf, to_read - first);
    }
    ring->read_pos = (ring->read_pos + to_read) % ring->capacity;
    ring->used -= to_read;
    return to_read;
}

bool SontMediaInit(SONT_MEDIA_PACKETIZER *ctx,
                   const SONT_SBC_PARAMS *params,
                   uint8_t bitpool,
                   uint16_t mtu,
                   uint32_t ssrc)
{
    unsigned frame_bytes;
    unsigned samples_per_frame;
    unsigned payload_budget;
    unsigned max_frames;
    SONT_SBC_ENCODER *encoder;

    if (ctx == NULL || params == NULL) {
        return false;
    }

    frame_bytes = SontSbcEncodedFrameBytes(params, bitpool);
    samples_per_frame = SontSbcSamplesPerFrame(params);
    if (frame_bytes == 0 || samples_per_frame == 0 ||
        frame_bytes > SONT_MEDIA_MAX_FRAME_BUF) {
        return false;
    }

    encoder = SontSbcEncoderCreate(params, bitpool);
    if (encoder == NULL) {
        return false;
    }

    if (mtu < (uint16_t)(SONT_RTP_HEADER_LEN + 1u + frame_bytes)) {
        payload_budget = frame_bytes;
    } else {
        payload_budget = (unsigned)mtu - SONT_RTP_HEADER_LEN - 1u;
    }
    if (payload_budget > SONT_MEDIA_MAX_FRAME_BUF) {
        payload_budget = SONT_MEDIA_MAX_FRAME_BUF;
    }

    max_frames = payload_budget / frame_bytes;
    if (max_frames == 0) {
        max_frames = 1;
    } else if (max_frames > 15u) {
        max_frames = 15u;
    }

    SONT_MEMSET(ctx, 0, sizeof(*ctx));
    ctx->encoder = encoder;
    ctx->sbc_params = *params;
    ctx->bitpool = bitpool;
    ctx->mtu = mtu;
    ctx->ssrc = ssrc;
    ctx->channels = SontSbcChannels(params);
    ctx->samples_per_frame = samples_per_frame;
    ctx->frame_bytes = frame_bytes;
    ctx->max_frames_per_packet = (uint8_t)max_frames;
    return true;
}

void SontMediaDestroy(SONT_MEDIA_PACKETIZER *ctx)
{
    if (ctx == NULL) {
        return;
    }
    if (ctx->encoder != NULL) {
        SontSbcEncoderDestroy(ctx->encoder);
        ctx->encoder = NULL;
    }
    SONT_MEMSET(ctx, 0, sizeof(*ctx));
}

int SontMediaFlush(SONT_MEDIA_PACKETIZER *ctx,
                   uint8_t *rtp_out,
                   size_t rtp_cap,
                   size_t *rtp_len)
{
    SONT_RTP_HEADER hdr;
    size_t payload_len;

    if (rtp_len != NULL) {
        *rtp_len = 0;
    }
    if (ctx == NULL || rtp_out == NULL || rtp_len == NULL) {
        return -1;
    }
    if (ctx->frames_pending == 0) {
        return 0;
    }
    if (rtp_cap < SONT_RTP_HEADER_LEN + 1u + ctx->frame_buf_used) {
        return -1;
    }

    SONT_MEMSET(&hdr, 0, sizeof(hdr));
    hdr.version = 2;
    hdr.marker = !ctx->sent_first;
    hdr.payload_type = SONT_RTP_PAYLOAD_TYPE;
    hdr.sequence = ctx->next_seq;
    hdr.timestamp = ctx->next_timestamp;
    hdr.ssrc = ctx->ssrc;

    if (SontRtpEncodeHeader(&hdr, rtp_out) != SONT_RTP_HEADER_LEN) {
        return -1;
    }
    payload_len = SontRtpSbcPayload(ctx->frame_buf,
                                    ctx->frame_buf_used,
                                    ctx->frames_pending,
                                    rtp_out + SONT_RTP_HEADER_LEN,
                                    rtp_cap - SONT_RTP_HEADER_LEN);
    if (payload_len == 0) {
        return -1;
    }

    *rtp_len = SONT_RTP_HEADER_LEN + payload_len;
    ctx->next_seq = (uint16_t)(ctx->next_seq + 1u);
    ctx->next_timestamp += (uint32_t)ctx->frames_pending * (uint32_t)ctx->samples_per_frame;
    ctx->sent_first = true;
    ctx->frames_pending = 0;
    ctx->frame_buf_used = 0;
    return 1;
}

int SontMediaPushPcmFrame(SONT_MEDIA_PACKETIZER *ctx,
                          const int16_t *pcm,
                          size_t samples_per_ch,
                          uint8_t *rtp_out,
                          size_t rtp_cap,
                          size_t *rtp_len)
{
    size_t written = 0;

    if (rtp_len != NULL) {
        *rtp_len = 0;
    }
    if (ctx == NULL || ctx->encoder == NULL || pcm == NULL ||
        rtp_out == NULL || rtp_len == NULL) {
        return -1;
    }
    if (samples_per_ch < ctx->samples_per_frame) {
        return -1;
    }
    if (ctx->frame_buf_used + ctx->frame_bytes > sizeof(ctx->frame_buf) ||
        ctx->frames_pending >= 15u) {
        return -1;
    }

    if (SontSbcEncode(ctx->encoder,
                      pcm,
                      ctx->samples_per_frame,
                      ctx->frame_buf + ctx->frame_buf_used,
                      sizeof(ctx->frame_buf) - ctx->frame_buf_used,
                      &written) != 0 || written == 0) {
        return -1;
    }

    ctx->frame_buf_used += written;
    ctx->frames_pending++;

    if (ctx->frames_pending >= ctx->max_frames_per_packet) {
        return SontMediaFlush(ctx, rtp_out, rtp_cap, rtp_len);
    }
    return 0;
}
