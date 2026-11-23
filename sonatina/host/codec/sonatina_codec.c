/* sonatina_codec: C ABI over the Sonatina SBC / LDAC encoders for the
 * user-mode host (SonatinaHost, phase U). Same encoder code as the kernel
 * driver (codec_sbc.c + libsbc, codec_ldac.c + libldac fixed point). */
#include "codec_sbc.h"
#include "codec_ldac.h"
#include "aacenc_lib.h"   /* Fraunhofer FDK AAC (third_party/fdk-aac) */
#include "freeaptx.h"     /* aptX / aptX HD (third_party/libfreeaptx, LGPL-2.1+) */
#include "sbc.h"          /* libsbc (Google, Apache-2.0): mSBC for HFP wide-band speech */
#include "rnnoise.h"      /* RNNoise v0.1.1 (Xiph/Mozilla, BSD-3-Clause): microphone noise reduction */
#define _USE_MATH_DEFINES
#include <math.h>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define SONT_API __declspec(dllexport)
#else
#define SONT_API __attribute__((visibility("default")))
#endif

SONT_API int sont_codec_version(void) { return 6; }

/* ------------------------------------------------------------------ LDAC */

SONT_API void *sont_ldac_create(unsigned sampling, unsigned channel_mode,
                                unsigned eqmid, unsigned mtu)
{
    SONT_LDAC_PARAMS p;
    p.sampling = sampling;
    p.channel_mode = (uint8_t)channel_mode;   /* 0 mono, 1 dual, 2 stereo */
    p.eqmid = (uint8_t)eqmid;                 /* 0 HQ, 1 SQ, 2 MQ */
    return SontLdacEncoderCreate(&p, (uint16_t)mtu);
}

SONT_API void sont_ldac_destroy(void *h) { SontLdacEncoderDestroy((SONT_LDAC_ENCODER *)h); }

SONT_API int sont_ldac_set_eqmid(void *h, unsigned eqmid)
{
    return SontLdacSetEqmid((SONT_LDAC_ENCODER *)h, (uint8_t)eqmid);
}

/* pcm: 128 frames of interleaved int16. Returns <0 on error, otherwise the
 * number of LDAC frames written to out (0 while libldac buffers). */
SONT_API int sont_ldac_encode(void *h, const int16_t *pcm, uint8_t *out,
                              unsigned capacity, unsigned *written)
{
    size_t w = 0;
    uint8_t frames = 0;
    if (written) {
        *written = 0;
    }
    if (SontLdacEncode((SONT_LDAC_ENCODER *)h, pcm, SONT_LDAC_PCM_SAMPLES_PER_PUSH,
                       out, capacity, &w, &frames) != 0) {
        return -1;
    }
    if (written) {
        *written = (unsigned)w;
    }
    return frames;
}

/* ------------------------------------------------------------------ SBC */

SONT_API void *sont_sbc_create(unsigned sampling, unsigned channel_mode, unsigned blocks,
                               unsigned subbands, unsigned allocation, unsigned bitpool)
{
    SONT_SBC_PARAMS p;
    p.sampling = sampling;
    p.channel_mode = (uint8_t)channel_mode;   /* 0 mono, 1 dual, 2 stereo, 3 joint */
    p.blocks = (uint8_t)blocks;
    p.subbands = (uint8_t)subbands;
    p.allocation = (uint8_t)allocation;       /* 0 SNR, 1 loudness */
    p.min_bitpool = 2;
    p.max_bitpool = (uint8_t)bitpool;
    return SontSbcEncoderCreate(&p, (uint8_t)bitpool);
}

SONT_API void sont_sbc_destroy(void *h) { SontSbcEncoderDestroy((SONT_SBC_ENCODER *)h); }

SONT_API unsigned sont_sbc_frame_bytes(unsigned sampling, unsigned channel_mode, unsigned blocks,
                                       unsigned subbands, unsigned allocation, unsigned bitpool)
{
    SONT_SBC_PARAMS p;
    p.sampling = sampling;
    p.channel_mode = (uint8_t)channel_mode;
    p.blocks = (uint8_t)blocks;
    p.subbands = (uint8_t)subbands;
    p.allocation = (uint8_t)allocation;
    p.min_bitpool = 2;
    p.max_bitpool = (uint8_t)bitpool;
    return SontSbcEncodedFrameBytes(&p, (uint8_t)bitpool);
}

/* pcm: blocks*subbands frames of interleaved int16 (128 for 16x8).
 * Returns bytes written, <0 on error. */
SONT_API int sont_sbc_encode(void *h, const int16_t *pcm, unsigned samples_per_ch,
                             uint8_t *out, unsigned capacity)
{
    size_t w = 0;
    if (SontSbcEncode((SONT_SBC_ENCODER *)h, pcm, samples_per_ch, out, capacity, &w) != 0) {
        return -1;
    }
    return (int)w;
}

/* ------------------------------------------------------------------ AAC */

/* AAC-LC in LATM (audioMuxVersion 0, StreamMuxConfig in every frame), the A2DP
 * packetization: one AudioMuxElement = one RTP payload. Settings follow Android's
 * a2dp_aac_encoder: afterburner on, peak bitrate bounded by the media MTU so that
 * no frame exceeds a packet. vbr_mode 0 = CBR (bitrate), 1..5 = FDK VBR modes. */
SONT_API void *sont_aac_create(unsigned sampling, unsigned channels, unsigned bitrate,
                               unsigned peak_bitrate, unsigned vbr_mode)
{
    HANDLE_AACENCODER h = NULL;
    AACENC_InfoStruct info;
    if (aacEncOpen(&h, 0x01 /* AAC only */, channels) != AACENC_OK) {
        return NULL;
    }
    if (aacEncoder_SetParam(h, AACENC_AOT, AOT_AAC_LC) != AACENC_OK ||
        aacEncoder_SetParam(h, AACENC_SAMPLERATE, sampling) != AACENC_OK ||
        aacEncoder_SetParam(h, AACENC_CHANNELMODE, channels == 1 ? MODE_1 : MODE_2) != AACENC_OK ||
        aacEncoder_SetParam(h, AACENC_CHANNELORDER, 1) != AACENC_OK ||
        aacEncoder_SetParam(h, AACENC_TRANSMUX, TT_MP4_LATM_MCP1) != AACENC_OK ||
        aacEncoder_SetParam(h, AACENC_HEADER_PERIOD, 1) != AACENC_OK ||
        aacEncoder_SetParam(h, AACENC_AUDIOMUXVER, 0) != AACENC_OK ||
        aacEncoder_SetParam(h, AACENC_GRANULE_LENGTH, 1024) != AACENC_OK ||
        aacEncoder_SetParam(h, AACENC_AFTERBURNER, 1) != AACENC_OK ||
        aacEncoder_SetParam(h, AACENC_BITRATEMODE, vbr_mode) != AACENC_OK) {
        aacEncClose(&h);
        return NULL;
    }
    if (vbr_mode == 0 && aacEncoder_SetParam(h, AACENC_BITRATE, bitrate) != AACENC_OK) {
        aacEncClose(&h);
        return NULL;
    }
    if (peak_bitrate && aacEncoder_SetParam(h, AACENC_PEAK_BITRATE, peak_bitrate) != AACENC_OK) {
        aacEncClose(&h);
        return NULL;
    }
    if (aacEncEncode(h, NULL, NULL, NULL, NULL) != AACENC_OK ||
        aacEncInfo(h, &info) != AACENC_OK || info.frameLength != 1024) {
        aacEncClose(&h);
        return NULL;
    }
    return h;
}

SONT_API void sont_aac_destroy(void *h)
{
    HANDLE_AACENCODER e = (HANDLE_AACENCODER)h;
    if (e) {
        aacEncClose(&e);
    }
}

/* Effective encoder settings, for logs: bitrate, peak bitrate, delay in samples. */
SONT_API int sont_aac_get(void *h, unsigned what)
{
    HANDLE_AACENCODER e = (HANDLE_AACENCODER)h;
    AACENC_InfoStruct info;
    switch (what) {
    case 0: return (int)aacEncoder_GetParam(e, AACENC_BITRATE);
    case 1: return (int)aacEncoder_GetParam(e, AACENC_PEAK_BITRATE);
    case 2: return aacEncInfo(e, &info) == AACENC_OK ? (int)info.nDelay : -1;
    default: return -1;
    }
}

/* pcm: `frames` frames of interleaved int16 (1024 per call keeps one AU out per call).
 * Returns the bytes of one AudioMuxElement written to out, 0 while the encoder fills
 * its look-ahead, <0 on error. */
SONT_API int sont_aac_encode(void *h, const int16_t *pcm, unsigned frames, unsigned channels,
                             uint8_t *out, unsigned capacity)
{
    HANDLE_AACENCODER e = (HANDLE_AACENCODER)h;
    AACENC_BufDesc in_desc = {0}, out_desc = {0};
    AACENC_InArgs in_args = {0};
    AACENC_OutArgs out_args = {0};
    void *in_ptr = (void *)pcm, *out_ptr = out;
    INT in_id = IN_AUDIO_DATA, in_size = (INT)(frames * channels * 2), in_el = 2;
    INT out_id = OUT_BITSTREAM_DATA, out_size = (INT)capacity, out_el = 1;

    in_desc.numBufs = 1;
    in_desc.bufs = &in_ptr;
    in_desc.bufferIdentifiers = &in_id;
    in_desc.bufSizes = &in_size;
    in_desc.bufElSizes = &in_el;
    out_desc.numBufs = 1;
    out_desc.bufs = &out_ptr;
    out_desc.bufferIdentifiers = &out_id;
    out_desc.bufSizes = &out_size;
    out_desc.bufElSizes = &out_el;
    in_args.numInSamples = (INT)(frames * channels);

    if (aacEncEncode(e, &in_desc, &out_desc, &in_args, &out_args) != AACENC_OK) {
        return -1;
    }
    return out_args.numOutBytes;
}

/* ------------------------------------------------------------------ aptX / aptX HD */

/* hd = 0: aptX (4 bytes per 4 stereo samples), 1: aptX HD (6 bytes). */
SONT_API void *sont_aptx_create(unsigned hd) { return aptx_init(hd ? 1 : 0); }

SONT_API void sont_aptx_destroy(void *h)
{
    if (h) {
        aptx_finish((struct aptx_context *)h);
    }
}

/* pcm: `frames` stereo frames of interleaved int16, frames a multiple of 4. The
 * codec takes 24-bit samples: int16 is widened (<< 8). Returns bytes written. */
SONT_API int sont_aptx_encode(void *h, const int16_t *pcm, unsigned frames,
                              uint8_t *out, unsigned capacity)
{
    unsigned char in[4 * 2 * 3 * 32];   /* 128 stereo frames of 24 bit per pass */
    unsigned done = 0, total = 0;
    if (h == NULL || (frames & 3) != 0) {
        return -1;
    }
    while (done < frames) {
        unsigned n = frames - done, i;
        size_t written = 0, used;
        if (n > 128) {
            n = 128;
        }
        for (i = 0; i < n * 2; i++) {
            int32_t v = (int32_t)pcm[done * 2 + i] * 256;
            in[i * 3 + 0] = (unsigned char)(v & 0xFF);
            in[i * 3 + 1] = (unsigned char)((v >> 8) & 0xFF);
            in[i * 3 + 2] = (unsigned char)((v >> 16) & 0xFF);
        }
        used = aptx_encode((struct aptx_context *)h, in, n * 6, out + total, capacity - total,
                           &written);
        if (used != n * 6) {
            return -1;   /* output buffer too small */
        }
        total += (unsigned)written;
        done += n;
    }
    return (int)total;
}

/* ------------------------------------------------------------------ volume */

/* Software volume for interleaved int16 PCM, gain in Q16 (65536 = 0 dB), ramped
 * linearly from g0 to g1 over the block to avoid zipper noise. Saturating. */
SONT_API void sont_pcm_gain(int16_t *pcm, unsigned frames, unsigned channels,
                            int g0_q16, int g1_q16)
{
    unsigned i, c;
    if (pcm == NULL || frames == 0 || channels == 0) {
        return;
    }
    for (i = 0; i < frames; i++) {
        int64_t g = (int64_t)g0_q16 + ((int64_t)(g1_q16 - g0_q16) * (int64_t)i) / (int64_t)frames;
        for (c = 0; c < channels; c++) {
            int64_t v = ((int64_t)pcm[i * channels + c] * g) >> 16;
            if (v > 32767) {
                v = 32767;
            } else if (v < -32768) {
                v = -32768;
            }
            pcm[i * channels + c] = (int16_t)v;
        }
    }
}

/* ------------------------------------------------------------------ mSBC (HFP) */

/* HFP 1.7+ wide-band speech: 16 kHz mono, 120 samples per frame. On the SCO link
 * each frame is sent as 60 bytes: H2 synchronisation header (0x01, then 0x08,
 * 0x38, 0xC8, 0xF8 in turn), the 57-byte mSBC frame, one padding byte. */
typedef struct {
    sbc_t enc;
    sbc_t dec;
    unsigned seq;
} SONT_MSBC;

static const uint8_t sont_h2[4] = {0x08, 0x38, 0xC8, 0xF8};

SONT_API void *sont_msbc_create(void)
{
    SONT_MSBC *m = (SONT_MSBC *)calloc(1, sizeof(SONT_MSBC));
    if (m) {
        sbc_reset(&m->enc);
        sbc_reset(&m->dec);
    }
    return m;
}

SONT_API void sont_msbc_destroy(void *h) { free(h); }

static void sont_msbc_frame(struct sbc_frame *f)
{
    memset(f, 0, sizeof(*f));
    f->msbc = true;
    f->freq = SBC_FREQ_16K;
    f->mode = SBC_MODE_MONO;
    f->bam = SBC_BAM_LOUDNESS;
    f->nblocks = 15;
    f->nsubbands = 8;
    f->bitpool = 26;
}

/* pcm: 120 samples. out: 60 bytes. Returns 60, <0 on error. */
SONT_API int sont_msbc_encode(void *h, const int16_t *pcm, uint8_t *out)
{
    SONT_MSBC *m = (SONT_MSBC *)h;
    struct sbc_frame f;
    if (m == NULL || pcm == NULL || out == NULL) {
        return -1;
    }
    sont_msbc_frame(&f);
    out[0] = 0x01;
    out[1] = sont_h2[m->seq & 3];
    m->seq++;
    if (sbc_encode(&m->enc, pcm, 1, NULL, 0, &f, out + 2, SBC_MSBC_SIZE) != 0) {
        return -1;
    }
    out[59] = 0x00;
    return 60;
}

/* frame: the 57 mSBC bytes (starting with 0xAD), or NULL for a lost frame (packet loss
 * concealment). pcm: 120 samples out. Returns 0, <0 if the frame is not valid mSBC
 * (pcm then holds concealment). */
SONT_API int sont_msbc_decode(void *h, const uint8_t *frame, int16_t *pcm)
{
    SONT_MSBC *m = (SONT_MSBC *)h;
    struct sbc_frame f;
    if (m == NULL || pcm == NULL) {
        return -1;
    }
    sont_msbc_frame(&f);
    if (frame != NULL && sbc_decode(&m->dec, frame, SBC_MSBC_SIZE, &f, pcm, 1, NULL, 0) == 0) {
        return 0;
    }
    sbc_decode(&m->dec, NULL, 0, &f, pcm, 1, NULL, 0);
    return frame == NULL ? 0 : -1;
}

/* ------------------------------------------------------------------ resampler */

/* Rational polyphase resampler (L/M), windowed-sinc low-pass, int16 in and out.
 * Used between Windows (48 kHz stereo) and the SCO link (8 or 16 kHz mono).
 * in_ch 2 is mixed to mono, out_ch 2 duplicates the mono result. */
typedef struct {
    unsigned L, M, taps, in_ch, out_ch;
    long t;           /* high-rate time of the next output, from the first new input */
    float *h;         /* L phases x taps */
    float *buf;       /* taps-1 history + current block */
    unsigned buf_cap;
} SONT_RS;

static unsigned sont_gcd(unsigned a, unsigned b)
{
    while (b) {
        unsigned t = a % b;
        a = b;
        b = t;
    }
    return a;
}

SONT_API void *sont_rs_create(unsigned in_rate, unsigned out_rate, unsigned in_ch, unsigned out_ch)
{
    SONT_RS *r;
    unsigned g, n, i, big;
    double fc, sum = 0.0;
    double *proto;
    if (in_rate == 0 || out_rate == 0 || in_ch < 1 || in_ch > 2 || out_ch < 1 || out_ch > 2) {
        return NULL;
    }
    r = (SONT_RS *)calloc(1, sizeof(SONT_RS));
    if (r == NULL) {
        return NULL;
    }
    g = sont_gcd(in_rate, out_rate);
    r->L = out_rate / g;
    r->M = in_rate / g;
    r->in_ch = in_ch;
    r->out_ch = out_ch;
    big = r->L > r->M ? r->L : r->M;
    r->taps = 24 * (big / r->L + 1);          /* taps per phase */
    n = r->taps * r->L;
    /* cutoff 0.45 x the lower of the two rates, as a fraction of the high rate */
    fc = 0.45 * (double)(in_rate < out_rate ? in_rate : out_rate) / ((double)in_rate * r->L);
    proto = (double *)malloc(sizeof(double) * n);
    r->h = (float *)malloc(sizeof(float) * n);
    if (proto == NULL || r->h == NULL) {
        free(proto);
        free(r->h);
        free(r);
        return NULL;
    }
    for (i = 0; i < n; i++) {
        double x = (double)i - (double)(n - 1) / 2.0;
        double s = x == 0.0 ? 2.0 * fc : sin(2.0 * M_PI * fc * x) / (M_PI * x);
        double w = 0.42 - 0.5 * cos(2.0 * M_PI * i / (n - 1)) + 0.08 * cos(4.0 * M_PI * i / (n - 1));
        proto[i] = s * w;
        sum += proto[i];
    }
    /* unity DC gain per phase: each output sees taps coefficients summing to ~sum/L */
    for (i = 0; i < n; i++) {
        unsigned p = i % r->L, j = i / r->L;
        r->h[p * r->taps + j] = (float)(proto[i] * r->L / sum);
    }
    free(proto);
    r->buf_cap = r->taps - 1;
    r->buf = (float *)calloc(r->buf_cap, sizeof(float));
    if (r->buf == NULL) {
        free(r->h);
        free(r);
        return NULL;
    }
    return r;
}

SONT_API void sont_rs_destroy(void *h)
{
    SONT_RS *r = (SONT_RS *)h;
    if (r) {
        free(r->h);
        free(r->buf);
        free(r);
    }
}

/* Upper bound of the output frames for in_frames input frames. */
SONT_API unsigned sont_rs_max_out(void *h, unsigned in_frames)
{
    SONT_RS *r = (SONT_RS *)h;
    return r ? (unsigned)(((unsigned long long)in_frames * r->L) / r->M + 2) : 0;
}

/* Returns the frames written to out (out_ch channels), <0 on error. */
SONT_API int sont_rs_process(void *h, const int16_t *in, unsigned in_frames,
                             int16_t *out, unsigned out_cap)
{
    SONT_RS *r = (SONT_RS *)h;
    unsigned hist, need, i, written = 0;
    float *x;
    if (r == NULL || (in == NULL && in_frames)) {
        return -1;
    }
    hist = r->taps - 1;
    need = hist + in_frames;
    if (need > r->buf_cap) {
        float *nb = (float *)realloc(r->buf, sizeof(float) * need);
        if (nb == NULL) {
            return -1;
        }
        r->buf = nb;
        r->buf_cap = need;
    }
    x = r->buf;
    for (i = 0; i < in_frames; i++) {
        x[hist + i] = r->in_ch == 2 ? ((float)in[2 * i] + (float)in[2 * i + 1]) * 0.5f : (float)in[i];
    }
    while (r->t >= 0 && (unsigned long)(r->t / r->L) < in_frames) {
        unsigned n = (unsigned)(r->t / r->L), p = (unsigned)(r->t % r->L), j;
        const float *c = r->h + p * r->taps;
        const float *s = x + hist + n;      /* s[-j] = input n - j */
        float acc = 0.0f;
        long v;
        if (written >= out_cap) {
            return -1;
        }
        for (j = 0; j < r->taps; j++) {
            acc += c[j] * s[-(long)j];
        }
        v = lrintf(acc);
        if (v > 32767) {
            v = 32767;
        } else if (v < -32768) {
            v = -32768;
        }
        if (r->out_ch == 2) {
            out[2 * written] = (int16_t)v;
            out[2 * written + 1] = (int16_t)v;
        } else {
            out[written] = (int16_t)v;
        }
        written++;
        r->t += r->M;
    }
    r->t -= (long)in_frames * r->L;
    memmove(x, x + in_frames, sizeof(float) * hist);
    return (int)written;
}

/* ------------------------------------------------------------------ voice (1.2.0) */

/* Cleaning of the headset microphone, 48 kHz mono, 480 samples (10 ms) per call:
 *   noise reduction  RNNoise; `strength` < 1 mixes some of the original back in (a natural
 *                    residue instead of a "underwater" sound). RNNoise output is one frame
 *                    late, so the original is delayed by one frame before mixing.
 *   AGC              the speech level (frames RNNoise calls voice) is brought towards
 *                    -20 dBFS: +3 dB/s up, 30 dB/s down, between -6 and +24 dB. Without AGC
 *                    the fixed gain is used.
 *   gate             200 ms without voice: down to -26 dB (in ~50 ms), open at once on voice.
 *   limiter          peaks above 29000 lower the gain at once, released over ~1 s.
 * flags: 1 = noise reduction, 2 = AGC, 4 = gate. With no flag only the fixed gain and the
 * limiter apply (no extra delay). */
#define SONT_VOICE_FRAME 480
#define SONT_VOICE_DENOISE 1
#define SONT_VOICE_AGC 2
#define SONT_VOICE_GATE 4

typedef struct {
    DenoiseState *rn;
    float dry[SONT_VOICE_FRAME];   /* previous input frame (aligned with RNNoise's output) */
    float gain;                    /* linear gain applied at the end of the last frame */
    float agc_db;
    float speech_db;
    float gate, gate_target;
    float limit;
    int quiet;
    float vad, in_peak, out_peak;
} SONT_VOICE;

SONT_API void *sont_voice_create(float start_gain_db)
{
    SONT_VOICE *v = (SONT_VOICE *)calloc(1, sizeof(SONT_VOICE));
    if (v == NULL) {
        return NULL;
    }
    v->rn = rnnoise_create(NULL);
    if (v->rn == NULL) {
        free(v);
        return NULL;
    }
    v->agc_db = start_gain_db;
    v->speech_db = -20.0f - start_gain_db;
    v->gain = powf(10.0f, start_gain_db / 20.0f);
    v->gate = v->gate_target = 1.0f;
    v->limit = 1.0f;
    return v;
}

SONT_API void sont_voice_destroy(void *h)
{
    SONT_VOICE *v = (SONT_VOICE *)h;
    if (v) {
        rnnoise_destroy(v->rn);
        free(v);
    }
}

/* Processes one frame in place; returns RNNoise's voice probability (0 without RNNoise). */
SONT_API float sont_voice_process(void *h, int16_t *pcm, unsigned flags, float strength,
                                  float fixed_gain_db)
{
    SONT_VOICE *v = (SONT_VOICE *)h;
    float x[SONT_VOICE_FRAME], d[SONT_VOICE_FRAME], y[SONT_VOICE_FRAME];
    float peak = 0.0f, sum = 0.0f, g0, g1, target_db, lim_peak;
    int i;
    if (v == NULL || pcm == NULL) {
        return 0.0f;
    }
    for (i = 0; i < SONT_VOICE_FRAME; i++) {
        x[i] = (float)pcm[i];
        if (fabsf(x[i]) > peak) {
            peak = fabsf(x[i]);
        }
    }
    v->in_peak = peak / 32768.0f;
    if (flags & (SONT_VOICE_DENOISE | SONT_VOICE_AGC | SONT_VOICE_GATE)) {
        v->vad = rnnoise_process_frame(v->rn, d, x);
        if (strength < 0.0f) {
            strength = 0.0f;
        } else if (strength > 1.0f) {
            strength = 1.0f;
        }
        for (i = 0; i < SONT_VOICE_FRAME; i++) {
            y[i] = (flags & SONT_VOICE_DENOISE) ? d[i] + (1.0f - strength) * (v->dry[i] - d[i]) : v->dry[i];
        }
        memcpy(v->dry, x, sizeof(x));
    } else {
        v->vad = 0.0f;
        memcpy(y, x, sizeof(x));
    }

    /* level of this frame, for the AGC */
    for (i = 0; i < SONT_VOICE_FRAME; i++) {
        sum += y[i] * y[i];
    }
    if (flags & SONT_VOICE_AGC) {
        float rms_db = 10.0f * log10f(sum / SONT_VOICE_FRAME / (32768.0f * 32768.0f) + 1e-12f);
        if (v->vad > 0.6f && rms_db > -65.0f) {
            v->speech_db += 0.05f * (rms_db - v->speech_db);
        }
        target_db = -20.0f - v->speech_db;
        if (target_db > 24.0f) {
            target_db = 24.0f;
        } else if (target_db < -6.0f) {
            target_db = -6.0f;
        }
        if (target_db > v->agc_db) {
            v->agc_db += fminf(0.03f, target_db - v->agc_db);
        } else {
            v->agc_db -= fminf(0.3f, v->agc_db - target_db);
        }
    } else {
        v->agc_db = fixed_gain_db;
    }

    /* gate */
    if (flags & SONT_VOICE_GATE) {
        if (v->vad > 0.5f) {
            v->quiet = 0;
            v->gate_target = 1.0f;
        } else if (++v->quiet > 20) {
            v->gate_target = 0.05f;
        }
    } else {
        v->gate_target = 1.0f;
    }
    if (v->gate_target > v->gate) {
        v->gate = v->gate_target;                       /* open at once */
    } else {
        v->gate += 0.2f * (v->gate_target - v->gate);   /* close over ~50 ms */
    }

    /* gain + limiter, ramped over the frame */
    g0 = v->gain;
    g1 = powf(10.0f, v->agc_db / 20.0f) * v->gate;
    peak = 0.0f;
    for (i = 0; i < SONT_VOICE_FRAME; i++) {
        if (fabsf(y[i]) > peak) {
            peak = fabsf(y[i]);
        }
    }
    lim_peak = peak * g1 * v->limit;
    if (lim_peak > 29000.0f) {
        v->limit *= 29000.0f / lim_peak;                /* at once */
    } else if (v->limit < 1.0f) {
        v->limit = fminf(1.0f, v->limit * 1.012f);      /* ~1 s back to 0 dB */
    }
    g1 *= v->limit;
    if (peak * g0 > 29000.0f) {                         /* a louder frame: no overshoot */
        g0 = 29000.0f / peak;
    }
    peak = 0.0f;
    for (i = 0; i < SONT_VOICE_FRAME; i++) {
        float g = g0 + (g1 - g0) * (float)i / SONT_VOICE_FRAME;
        float s = y[i] * g;
        long q;
        if (s > 32767.0f) {
            s = 32767.0f;
        } else if (s < -32768.0f) {
            s = -32768.0f;
        }
        q = lrintf(s);
        pcm[i] = (int16_t)q;
        if (fabsf(s) > peak) {
            peak = fabsf(s);
        }
    }
    v->gain = g1;
    v->out_peak = peak / 32768.0f;
    return v->vad;
}

/* 0 voice probability, 1 AGC gain dB, 2 gate (0..1), 3 input peak, 4 output peak (0..1),
 * 5 limiter (0..1) */
SONT_API float sont_voice_get(void *h, unsigned what)
{
    SONT_VOICE *v = (SONT_VOICE *)h;
    if (v == NULL) {
        return 0.0f;
    }
    switch (what) {
    case 0: return v->vad;
    case 1: return v->agc_db;
    case 2: return v->gate;
    case 3: return v->in_peak;
    case 4: return v->out_peak;
    case 5: return v->limit;
    default: return 0.0f;
    }
}
