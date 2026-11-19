/* sonatina_codec: C ABI over the Sonatina SBC / LDAC encoders for the
 * user-mode host (SonatinaHost, phase U). Same encoder code as the kernel
 * driver (codec_sbc.c + libsbc, codec_ldac.c + libldac fixed point). */
#include "codec_sbc.h"
#include "codec_ldac.h"

#ifdef _WIN32
#define SONT_API __declspec(dllexport)
#else
#define SONT_API __attribute__((visibility("default")))
#endif

SONT_API int sont_codec_version(void) { return 1; }

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
