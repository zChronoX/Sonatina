#include "codec_sbc.h"

#include "portable.h"

#ifdef SONATINA_WITH_LIBSBC
#include <sbc/sbc.h>

struct SONT_SBC_ENCODER {
    sbc_t sbc;
    uint8_t bitpool;
    unsigned channels;
};

static int SontSbcFrequencyIndex(uint32_t hz)
{
    switch (hz) {
    case 16000: return SBC_FREQ_16000;
    case 32000: return SBC_FREQ_32000;
    case 44100: return SBC_FREQ_44100;
    case 48000: return SBC_FREQ_48000;
    default:    return -1;
    }
}

static int SontSbcBlockIndex(uint8_t blocks)
{
    switch (blocks) {
    case 4:  return SBC_BLK_4;
    case 8:  return SBC_BLK_8;
    case 12: return SBC_BLK_12;
    case 16: return SBC_BLK_16;
    default: return -1;
    }
}

SONT_SBC_ENCODER *SontSbcEncoderCreate(const SONT_SBC_PARAMS *params, uint8_t bitpool)
{
    SONT_SBC_ENCODER *encoder;
    int freq, blocks, subbands;

    if (params == NULL) {
        return NULL;
    }
    freq = SontSbcFrequencyIndex(params->sampling);
    blocks = SontSbcBlockIndex(params->blocks);
    subbands = (params->subbands == 8) ? SBC_SB_8 : SBC_SB_4;
    if (freq < 0 || blocks < 0 || params->channel_mode > 3) {
        return NULL;
    }
    if (bitpool < params->min_bitpool || bitpool > params->max_bitpool) {
        return NULL;
    }

    encoder = (SONT_SBC_ENCODER *)SONT_ALLOC(sizeof(*encoder));
    if (encoder == NULL) {
        return NULL;
    }
    if (sbc_init(&encoder->sbc, 0) != 0) {
        SONT_FREE(encoder);
        return NULL;
    }
    encoder->sbc.frequency = (unsigned)freq;
    encoder->sbc.blocks = (unsigned)blocks;
    encoder->sbc.subbands = (unsigned)subbands;
    encoder->sbc.mode = params->channel_mode;
    encoder->sbc.allocation = (params->allocation == 1) ? SBC_AM_LOUDNESS : SBC_AM_SNR;
    encoder->sbc.bitpool = bitpool;
    encoder->bitpool = bitpool;
    encoder->channels = SontSbcChannels(params);
    return encoder;
}

void SontSbcEncoderDestroy(SONT_SBC_ENCODER *encoder)
{
    if (encoder != NULL) {
        sbc_finish(&encoder->sbc);
        SONT_FREE(encoder);
    }
}

int SontSbcEncode(SONT_SBC_ENCODER *encoder, const int16_t *pcm, size_t samples,
                  uint8_t *out, size_t capacity, size_t *written)
{
    long produced;

    if (encoder == NULL || pcm == NULL || out == NULL || written == NULL) {
        return -1;
    }
    produced = (long)sbc_encode(&encoder->sbc, pcm,
                                samples * encoder->channels * sizeof(int16_t),
                                out, capacity);
    if (produced < 0) {
        return -1;
    }
    *written = (size_t)produced;
    return 0;
}

#else /* SONATINA_WITH_LIBSBC not defined: build a compiling stub. */

struct SONT_SBC_ENCODER {
    uint8_t bitpool;
    unsigned channels;
};

SONT_SBC_ENCODER *SontSbcEncoderCreate(const SONT_SBC_PARAMS *params, uint8_t bitpool)
{
    SONT_SBC_ENCODER *encoder;
    if (params == NULL) {
        return NULL;
    }
    encoder = (SONT_SBC_ENCODER *)SONT_ALLOC(sizeof(*encoder));
    if (encoder != NULL) {
        encoder->bitpool = bitpool;
        encoder->channels = SontSbcChannels(params);
    }
    return encoder;
}

void SontSbcEncoderDestroy(SONT_SBC_ENCODER *encoder)
{
    SONT_FREE(encoder);
}

int SontSbcEncode(SONT_SBC_ENCODER *encoder, const int16_t *pcm, size_t samples,
                  uint8_t *out, size_t capacity, size_t *written)
{
    (void)encoder; (void)pcm; (void)samples;
    if (out == NULL || written == NULL || capacity == 0) {
        return -1;
    }
    out[0] = 0; /* placeholder: link libsbc for real audio */
    *written = 1;
    return 0;
}

#endif

unsigned SontSbcChannels(const SONT_SBC_PARAMS *params)
{
    return (params->channel_mode == 0) ? 1u : 2u;
}

unsigned SontSbcSamplesPerFrame(const SONT_SBC_PARAMS *params)
{
    return (unsigned)params->blocks * (unsigned)params->subbands;
}

unsigned SontSbcFrameBytes(const SONT_SBC_PARAMS *params, uint8_t bitpool)
{
    unsigned channels = SontSbcChannels(params);
    unsigned size = 4u + (4u * params->subbands * channels) / 8u;

    if (channels == 2 && params->channel_mode == 3) {
        size += params->subbands + ((unsigned)params->blocks * bitpool) / 8u;
    } else if (channels == 2) {
        size += ((unsigned)params->blocks * 2u * bitpool) / 8u;
    } else {
        size += ((unsigned)params->blocks * bitpool) / 8u;
    }
    return size;
}

unsigned SontSbcBitrateKbpsInt(const SONT_SBC_PARAMS *params, uint8_t bitpool)
{
    uint64_t samples_per_frame = (uint64_t)SontSbcSamplesPerFrame(params);
    uint64_t frame_bytes = (uint64_t)SontSbcFrameBytes(params, bitpool);
    uint64_t denom;

    if (samples_per_frame == 0) {
        return 0;
    }
    denom = samples_per_frame * 1000ULL;
    return (unsigned)((frame_bytes * 8ULL * (uint64_t)params->sampling + denom / 2ULL) / denom);
}

#ifndef SONATINA_KERNEL
double SontSbcBitrateKbps(const SONT_SBC_PARAMS *params, uint8_t bitpool)
{
    double frames_per_second =
        (double)params->sampling / (double)SontSbcSamplesPerFrame(params);
    return (double)SontSbcFrameBytes(params, bitpool) * 8.0 * frames_per_second / 1000.0;
}
#endif
