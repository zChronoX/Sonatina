#include "codec_sbc.h"

#include "portable.h"

#ifdef SONATINA_WITH_LIBSBC
#include <sbc.h>

struct SONT_SBC_ENCODER {
    sbc_t sbc;
    struct sbc_frame frame;
    unsigned channels;
    unsigned samples_per_frame;
    unsigned frame_bytes;
};

static int SontSbcFrequencyEnum(uint32_t hz, enum sbc_freq *out)
{
    switch (hz) {
    case 16000: *out = SBC_FREQ_16K;  return 0;
    case 32000: *out = SBC_FREQ_32K;  return 0;
    case 44100: *out = SBC_FREQ_44K1; return 0;
    case 48000: *out = SBC_FREQ_48K;  return 0;
    default:    return -1;
    }
}

static int SontSbcModeEnum(uint8_t channel_mode, enum sbc_mode *out)
{
    switch (channel_mode) {
    case 0:  *out = SBC_MODE_MONO;         return 0;
    case 1:  *out = SBC_MODE_DUAL_CHANNEL; return 0;
    case 2:  *out = SBC_MODE_STEREO;       return 0;
    case 3:  *out = SBC_MODE_JOINT_STEREO; return 0;
    default: return -1;
    }
}

SONT_SBC_ENCODER *SontSbcEncoderCreate(const SONT_SBC_PARAMS *params, uint8_t bitpool)
{
    SONT_SBC_ENCODER *encoder;
    enum sbc_freq freq;
    enum sbc_mode mode;
    unsigned frame_bytes;

    if (params == NULL) {
        return NULL;
    }
    if (SontSbcFrequencyEnum(params->sampling, &freq) != 0 ||
        SontSbcModeEnum(params->channel_mode, &mode) != 0) {
        return NULL;
    }
    if (params->blocks != 4 && params->blocks != 8 &&
        params->blocks != 12 && params->blocks != 16) {
        return NULL;
    }
    if (params->subbands != 4 && params->subbands != 8) {
        return NULL;
    }
    if (bitpool < params->min_bitpool || bitpool > params->max_bitpool) {
        return NULL;
    }

    encoder = (SONT_SBC_ENCODER *)SONT_ALLOC(sizeof(*encoder));
    if (encoder == NULL) {
        return NULL;
    }
    SONT_MEMSET(encoder, 0, sizeof(*encoder));
    sbc_reset(&encoder->sbc);

    encoder->frame.msbc = false;
    encoder->frame.freq = freq;
    encoder->frame.mode = mode;
    encoder->frame.bam = (params->allocation == 1) ? SBC_BAM_LOUDNESS : SBC_BAM_SNR;
    encoder->frame.nblocks = params->blocks;
    encoder->frame.nsubbands = params->subbands;
    encoder->frame.bitpool = bitpool;

    frame_bytes = sbc_get_frame_size(&encoder->frame);
    if (frame_bytes == 0) {
        SONT_FREE(encoder);
        return NULL;
    }
    encoder->channels = SontSbcChannels(params);
    encoder->samples_per_frame = SontSbcSamplesPerFrame(params);
    encoder->frame_bytes = frame_bytes;
    return encoder;
}

void SontSbcEncoderDestroy(SONT_SBC_ENCODER *encoder)
{
    if (encoder != NULL) {
        SONT_FREE(encoder);
    }
}

int SontSbcSetBitpool(SONT_SBC_ENCODER *encoder, const SONT_SBC_PARAMS *params, uint8_t bitpool)
{
    unsigned frame_bytes;

    if (encoder == NULL || params == NULL) {
        return -1;
    }
    if (bitpool < params->min_bitpool || bitpool > params->max_bitpool) {
        return -1;
    }
    encoder->frame.bitpool = bitpool;
    frame_bytes = sbc_get_frame_size(&encoder->frame);
    if (frame_bytes == 0) {
        return -1;
    }
    encoder->frame_bytes = frame_bytes;
    return 0;
}

int SontSbcEncode(SONT_SBC_ENCODER *encoder, const int16_t *pcm, size_t samples,
                  uint8_t *out, size_t capacity, size_t *written)
{
    const int16_t *pcmr;
    int pitchl;
    int pitchr;

    if (encoder == NULL || pcm == NULL || out == NULL || written == NULL) {
        return -1;
    }
    *written = 0;
    if (samples < encoder->samples_per_frame || capacity < encoder->frame_bytes) {
        return -1;
    }

    if (encoder->channels == 1) {
        pitchl = 1;
        pcmr = NULL;
        pitchr = 0;
    } else {
        pitchl = 2;
        pcmr = pcm + 1;
        pitchr = 2;
    }

    if (sbc_encode(&encoder->sbc, pcm, pitchl, pcmr, pitchr,
                   &encoder->frame, out, encoder->frame_bytes) != 0) {
        return -1;
    }
    *written = encoder->frame_bytes;
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

int SontSbcSetBitpool(SONT_SBC_ENCODER *encoder, const SONT_SBC_PARAMS *params, uint8_t bitpool)
{
    if (encoder == NULL || params == NULL) {
        return -1;
    }
    if (bitpool < params->min_bitpool || bitpool > params->max_bitpool) {
        return -1;
    }
    encoder->bitpool = bitpool;
    return 0;
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

unsigned SontSbcEncodedFrameBytes(const SONT_SBC_PARAMS *params, uint8_t bitpool)
{
#ifdef SONATINA_WITH_LIBSBC
    struct sbc_frame frame;
    enum sbc_freq freq;
    enum sbc_mode mode;

    if (params == NULL ||
        SontSbcFrequencyEnum(params->sampling, &freq) != 0 ||
        SontSbcModeEnum(params->channel_mode, &mode) != 0) {
        return 0;
    }
    SONT_MEMSET(&frame, 0, sizeof(frame));
    frame.msbc = false;
    frame.freq = freq;
    frame.mode = mode;
    frame.bam = (params->allocation == 1) ? SBC_BAM_LOUDNESS : SBC_BAM_SNR;
    frame.nblocks = params->blocks;
    frame.nsubbands = params->subbands;
    frame.bitpool = bitpool;
    return sbc_get_frame_size(&frame);
#else
    return SontSbcFrameBytes(params, bitpool);
#endif
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
