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

/* Drops up to `len` of the oldest bytes; returns the number dropped. */
size_t SontPcmRingDiscard(SONT_PCM_RING *ring, size_t len);

/* True when the newest `max_bytes` of interleaved int16 PCM in the ring hold a
 * sample louder than `threshold` (used to tell real playback from an open but
 * silent stream). */
bool SontPcmRingHasSignal(const SONT_PCM_RING *ring, size_t max_bytes, int16_t threshold);

/* ------------------------------------------------------------------ PCM pull
 *
 * Converts the WaveRT stream format (rate / channels written into the ring by
 * the audio engine) to the negotiated codec format, behind a small jitter
 * buffer. The pull always fills the requested number of frames: real audio
 * when enough is buffered, digital silence otherwise. Streaming silence keeps
 * the A2DP link alive when nothing is playing, instead of suspending it (many
 * headsets go quiet until power-cycled when a suspended stream is resumed).
 */
typedef struct _SONT_RESAMPLER {
    uint32_t in_rate;
    uint32_t out_rate;
    unsigned in_channels;      /* 1 or 2 */
    unsigned out_channels;     /* 1 or 2 */
    uint32_t step_q16;         /* in_rate / out_rate, Q16.16 */
    uint32_t frac_q16;         /* position between prev and next, Q16.16 */
    int16_t prev[2];
    int16_t next[2];
    bool primed;
} SONT_RESAMPLER;

typedef struct _SONT_PCM_PULL {
    SONT_RESAMPLER rs;
    bool playing;              /* false while (re)filling the jitter buffer */
    uint32_t prefill_frames;   /* input frames buffered before playback (re)starts */
    uint32_t max_frames;       /* backlog above this is dropped to bound latency */
    uint32_t underruns;
    uint64_t dropped_frames;
} SONT_PCM_PULL;

void SontResamplerInit(SONT_RESAMPLER *rs, uint32_t in_rate, unsigned in_channels,
                       uint32_t out_rate, unsigned out_channels);
/* Produces up to `out_frames` interleaved frames; fewer when the ring runs dry. */
size_t SontResamplerPull(SONT_RESAMPLER *rs, SONT_PCM_RING *ring,
                         int16_t *out, size_t out_frames);

/* prefill_ms / max_ms are converted with the input rate. */
void SontPcmPullInit(SONT_PCM_PULL *pull, uint32_t in_rate, unsigned in_channels,
                     uint32_t out_rate, unsigned out_channels,
                     uint32_t prefill_ms, uint32_t max_ms);
/* Always writes `out_frames` frames (silence where no audio is available) and
 * returns how many of them carry real audio. */
size_t SontPcmPull(SONT_PCM_PULL *pull, SONT_PCM_RING *ring,
                   int16_t *out, size_t out_frames);

/* ------------------------------------------------------------------ media clock
 *
 * Paces encoding on the monotonic clock: tells the sender how many codec
 * frames are due, with a small send-ahead lead, and skips (instead of bursting)
 * when the sender was starved for longer than `max_burst` frames. */
typedef struct _SONT_MEDIA_CLOCK {
    uint64_t origin_us;
    uint64_t samples_done;
    uint32_t rate;
    uint32_t lead_us;
    uint32_t skips;
} SONT_MEDIA_CLOCK;

void SontMediaClockStart(SONT_MEDIA_CLOCK *clk, uint64_t now_us, uint32_t rate, uint32_t lead_us);
uint32_t SontMediaClockFramesDue(SONT_MEDIA_CLOCK *clk, uint64_t now_us,
                                 uint32_t samples_per_frame, uint32_t max_burst);
void SontMediaClockConsume(SONT_MEDIA_CLOCK *clk, uint32_t samples);
/* Microseconds until the next frame becomes due (0 when one is already due). */
uint32_t SontMediaClockUsUntilNext(const SONT_MEDIA_CLOCK *clk, uint64_t now_us,
                                   uint32_t samples_per_frame);

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
