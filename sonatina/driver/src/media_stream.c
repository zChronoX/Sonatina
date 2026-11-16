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

size_t SontPcmRingDiscard(SONT_PCM_RING *ring, size_t len)
{
    size_t n;

    if (ring == NULL || ring->capacity == 0 || len == 0) {
        return 0;
    }
    n = (len < ring->used) ? len : ring->used;
    ring->read_pos = (ring->read_pos + n) % ring->capacity;
    ring->used -= n;
    return n;
}

bool SontPcmRingHasSignal(const SONT_PCM_RING *ring, size_t max_bytes, int16_t threshold)
{
    size_t n;
    size_t i;
    size_t pos;

    if (ring == NULL || ring->buf == NULL || ring->capacity < 2 || ring->used < 2) {
        return false;
    }
    n = (ring->used < max_bytes) ? ring->used : max_bytes;
    n &= ~(size_t)1;
    pos = ring->write_pos;
    for (i = 0; i < n; i += 2) {
        uint8_t lo;
        uint8_t hi;
        int16_t v;

        pos = (pos >= 2) ? pos - 2 : pos + ring->capacity - 2;
        lo = ring->buf[pos];
        hi = ring->buf[(pos + 1) % ring->capacity];
        v = (int16_t)(uint16_t)((uint16_t)lo | ((uint16_t)hi << 8));
        if (v > threshold || v < (int16_t)-threshold) {
            return true;
        }
    }
    return false;
}

/* ------------------------------------------------------------------ resampler */

static bool sont_read_frame(SONT_RESAMPLER *rs, SONT_PCM_RING *ring, int16_t out[2])
{
    int16_t in[2];
    size_t bytes = (size_t)rs->in_channels * sizeof(int16_t);

    if (SontPcmRingAvailable(ring) < bytes ||
        SontPcmRingRead(ring, (uint8_t *)in, bytes) != bytes) {
        return false;
    }
    if (rs->in_channels == rs->out_channels) {
        out[0] = in[0];
        out[1] = (rs->in_channels == 2) ? in[1] : in[0];
    } else if (rs->in_channels == 2) {
        out[0] = (int16_t)(((int32_t)in[0] + (int32_t)in[1]) / 2);  /* downmix */
        out[1] = out[0];
    } else {
        out[0] = in[0];
        out[1] = in[0];                                             /* upmix */
    }
    return true;
}

void SontResamplerInit(SONT_RESAMPLER *rs, uint32_t in_rate, unsigned in_channels,
                       uint32_t out_rate, unsigned out_channels)
{
    if (rs == NULL) {
        return;
    }
    SONT_MEMSET(rs, 0, sizeof(*rs));
    rs->in_rate = (in_rate != 0) ? in_rate : 48000u;
    rs->out_rate = (out_rate != 0) ? out_rate : rs->in_rate;
    rs->in_channels = (in_channels == 1) ? 1u : 2u;
    rs->out_channels = (out_channels == 1) ? 1u : 2u;
    rs->step_q16 = (uint32_t)(((uint64_t)rs->in_rate << 16) / rs->out_rate);
    if (rs->step_q16 == 0) {
        rs->step_q16 = 1;
    }
}

size_t SontResamplerPull(SONT_RESAMPLER *rs, SONT_PCM_RING *ring,
                         int16_t *out, size_t out_frames)
{
    size_t i;
    unsigned c;

    if (rs == NULL || ring == NULL || out == NULL) {
        return 0;
    }
    if (!rs->primed) {
        if (SontPcmRingAvailable(ring) < 2u * rs->in_channels * sizeof(int16_t)) {
            return 0;
        }
        (void)sont_read_frame(rs, ring, rs->prev);
        (void)sont_read_frame(rs, ring, rs->next);
        rs->frac_q16 = 0;
        rs->primed = true;
    }
    for (i = 0; i < out_frames; i++) {
        while (rs->frac_q16 >= 0x10000u) {
            int16_t f[2];
            if (!sont_read_frame(rs, ring, f)) {
                return i;
            }
            rs->prev[0] = rs->next[0];
            rs->prev[1] = rs->next[1];
            rs->next[0] = f[0];
            rs->next[1] = f[1];
            rs->frac_q16 -= 0x10000u;
        }
        for (c = 0; c < rs->out_channels; c++) {
            int32_t a = rs->prev[c];
            int32_t b = rs->next[c];
            out[i * rs->out_channels + c] =
                (int16_t)(a + (int32_t)(((int64_t)(b - a) * (int64_t)rs->frac_q16) >> 16));
        }
        rs->frac_q16 += rs->step_q16;
    }
    return out_frames;
}

void SontPcmPullInit(SONT_PCM_PULL *pull, uint32_t in_rate, unsigned in_channels,
                     uint32_t out_rate, unsigned out_channels,
                     uint32_t prefill_ms, uint32_t max_ms)
{
    if (pull == NULL) {
        return;
    }
    SONT_MEMSET(pull, 0, sizeof(*pull));
    SontResamplerInit(&pull->rs, in_rate, in_channels, out_rate, out_channels);
    pull->prefill_frames = (uint32_t)(((uint64_t)pull->rs.in_rate * prefill_ms) / 1000u);
    pull->max_frames = (uint32_t)(((uint64_t)pull->rs.in_rate * max_ms) / 1000u);
    if (pull->max_frames < pull->prefill_frames * 2u) {
        pull->max_frames = pull->prefill_frames * 2u;
    }
}

size_t SontPcmPull(SONT_PCM_PULL *pull, SONT_PCM_RING *ring,
                   int16_t *out, size_t out_frames)
{
    size_t frame_bytes;
    size_t avail_frames;
    size_t got = 0;

    if (pull == NULL || out == NULL || out_frames == 0) {
        return 0;
    }
    frame_bytes = (size_t)pull->rs.in_channels * sizeof(int16_t);
    avail_frames = (ring != NULL) ? SontPcmRingAvailable(ring) / frame_bytes : 0;

    if (avail_frames > pull->max_frames) {
        /* The producer ran ahead (e.g. after a stall): drop the oldest audio so
         * latency stays bounded. */
        size_t drop = avail_frames - pull->prefill_frames;
        pull->dropped_frames += SontPcmRingDiscard(ring, drop * frame_bytes) / frame_bytes;
        avail_frames -= drop;
    }
    if (!pull->playing && avail_frames >= pull->prefill_frames && avail_frames >= 2u) {
        pull->playing = true;
        pull->rs.primed = false;
    }
    if (pull->playing) {
        got = SontResamplerPull(&pull->rs, ring, out, out_frames);
        if (got < out_frames) {
            pull->playing = false;
            pull->underruns++;
        }
    }
    if (got < out_frames) {
        SONT_MEMSET(out + got * pull->rs.out_channels, 0,
                    (out_frames - got) * pull->rs.out_channels * sizeof(int16_t));
    }
    return got;
}

/* ------------------------------------------------------------------ media clock */

void SontMediaClockStart(SONT_MEDIA_CLOCK *clk, uint64_t now_us, uint32_t rate, uint32_t lead_us)
{
    if (clk == NULL) {
        return;
    }
    SONT_MEMSET(clk, 0, sizeof(*clk));
    clk->origin_us = now_us;
    clk->rate = (rate != 0) ? rate : 48000u;
    clk->lead_us = lead_us;
}

static uint64_t sont_clock_target(const SONT_MEDIA_CLOCK *clk, uint64_t now_us)
{
    uint64_t elapsed = (now_us > clk->origin_us) ? (now_us - clk->origin_us) : 0;
    return ((elapsed + clk->lead_us) * (uint64_t)clk->rate) / 1000000u;
}

uint32_t SontMediaClockFramesDue(SONT_MEDIA_CLOCK *clk, uint64_t now_us,
                                 uint32_t samples_per_frame, uint32_t max_burst)
{
    uint64_t target;
    uint64_t frames;

    if (clk == NULL || samples_per_frame == 0) {
        return 0;
    }
    target = sont_clock_target(clk, now_us);
    if (target <= clk->samples_done) {
        return 0;
    }
    frames = (target - clk->samples_done) / samples_per_frame;
    if (max_burst != 0 && frames > max_burst) {
        clk->samples_done += (frames - max_burst) * samples_per_frame;
        clk->skips++;
        frames = max_burst;
    }
    return (uint32_t)frames;
}

void SontMediaClockConsume(SONT_MEDIA_CLOCK *clk, uint32_t samples)
{
    if (clk != NULL) {
        clk->samples_done += samples;
    }
}

uint32_t SontMediaClockUsUntilNext(const SONT_MEDIA_CLOCK *clk, uint64_t now_us,
                                   uint32_t samples_per_frame)
{
    uint64_t target;
    uint64_t need;
    uint64_t due_us;
    uint64_t elapsed;

    if (clk == NULL || clk->rate == 0) {
        return 0;
    }
    target = sont_clock_target(clk, now_us);
    need = clk->samples_done + samples_per_frame;
    if (target >= need) {
        return 0;
    }
    /* Time at which `need` samples are due, measured from origin minus lead. */
    due_us = (need * 1000000u + clk->rate - 1u) / clk->rate;
    elapsed = (now_us > clk->origin_us) ? (now_us - clk->origin_us) : 0;
    elapsed += clk->lead_us;
    return (due_us > elapsed) ? (uint32_t)(due_us - elapsed) : 0;
}

static uint8_t sont_calc_max_frames(unsigned frame_bytes, uint16_t mtu)
{
    unsigned payload_budget;
    unsigned max_frames;

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
    return (uint8_t)max_frames;
}

bool SontMediaInit(SONT_MEDIA_PACKETIZER *ctx,
                   const SONT_SBC_PARAMS *params,
                   uint8_t bitpool,
                   uint16_t mtu,
                   uint32_t ssrc)
{
    unsigned frame_bytes;
    unsigned samples_per_frame;
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

    SONT_MEMSET(ctx, 0, sizeof(*ctx));
    ctx->encoder = encoder;
    ctx->sbc_params = *params;
    ctx->bitpool = bitpool;
    ctx->mtu = mtu;
    ctx->ssrc = ssrc;
    ctx->channels = SontSbcChannels(params);
    ctx->samples_per_frame = samples_per_frame;
    ctx->frame_bytes = frame_bytes;
    ctx->max_frames_per_packet = sont_calc_max_frames(frame_bytes, mtu);
    return true;
}

bool SontMediaSetBitpool(SONT_MEDIA_PACKETIZER *ctx, uint8_t bitpool)
{
    unsigned frame_bytes;

    if (ctx == NULL || ctx->encoder == NULL || ctx->frames_pending != 0) {
        return false;
    }
    if (bitpool < ctx->sbc_params.min_bitpool || bitpool > ctx->sbc_params.max_bitpool) {
        return false;
    }
    if (bitpool == ctx->bitpool) {
        return true;
    }

    frame_bytes = SontSbcEncodedFrameBytes(&ctx->sbc_params, bitpool);
    if (frame_bytes == 0 || frame_bytes > SONT_MEDIA_MAX_FRAME_BUF) {
        return false;
    }
    if (SontSbcSetBitpool(ctx->encoder, &ctx->sbc_params, bitpool) != 0) {
        return false;
    }

    ctx->bitpool = bitpool;
    ctx->frame_bytes = frame_bytes;
    ctx->max_frames_per_packet = sont_calc_max_frames(frame_bytes, ctx->mtu);
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
