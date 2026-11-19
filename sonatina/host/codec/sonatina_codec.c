/* sonatina_codec: C ABI over the Sonatina SBC / LDAC encoders for the
 * user-mode host (SonatinaHost, phase U). Same encoder code as the kernel
 * driver (codec_sbc.c + libsbc, codec_ldac.c + libldac fixed point). */
#include "codec_sbc.h"
#include "codec_ldac.h"
#include "aacenc_lib.h"   /* Fraunhofer FDK AAC (third_party/fdk-aac) */
#include "freeaptx.h"     /* aptX / aptX HD (third_party/libfreeaptx, LGPL-2.1+) */

#ifdef _WIN32
#define SONT_API __declspec(dllexport)
#else
#define SONT_API __attribute__((visibility("default")))
#endif

SONT_API int sont_codec_version(void) { return 4; }

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
