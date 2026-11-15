/* SBC parameter model and encoder wrapper.
 *
 * The bitrate/frame maths is the SBC specification formula, the same one used by
 * the Python reference (sonatina/sbc.py). The encoder is provided by libsbc
 * (AOSP external/bluetooth/sbc, Apache-2.0); build with SONATINA_WITH_LIBSBC.
 */
#ifndef SONATINA_CODEC_SBC_H
#define SONATINA_CODEC_SBC_H

#include "portable.h"

typedef struct _SONT_SBC_PARAMS {
    uint32_t sampling;      /* Hz: 16000/32000/44100/48000 */
    uint8_t  channel_mode;  /* 0 mono, 1 dual, 2 stereo, 3 joint */
    uint8_t  blocks;        /* 4/8/12/16 */
    uint8_t  subbands;      /* 4/8 */
    uint8_t  allocation;    /* 0 SNR, 1 loudness */
    uint8_t  min_bitpool;
    uint8_t  max_bitpool;
} SONT_SBC_PARAMS;

unsigned SontSbcChannels(const SONT_SBC_PARAMS *params);
unsigned SontSbcSamplesPerFrame(const SONT_SBC_PARAMS *params);
unsigned SontSbcFrameBytes(const SONT_SBC_PARAMS *params, uint8_t bitpool);
unsigned SontSbcBitrateKbpsInt(const SONT_SBC_PARAMS *params, uint8_t bitpool);
#ifndef SONATINA_KERNEL
double   SontSbcBitrateKbps(const SONT_SBC_PARAMS *params, uint8_t bitpool);
#endif

typedef struct SONT_SBC_ENCODER SONT_SBC_ENCODER;

SONT_SBC_ENCODER *SontSbcEncoderCreate(const SONT_SBC_PARAMS *params, uint8_t bitpool);
void SontSbcEncoderDestroy(SONT_SBC_ENCODER *encoder);

/* Encode interleaved int16 PCM. `samples` is the number of samples per channel.
 * Returns 0 on success and stores the number of bytes written. */
int SontSbcEncode(SONT_SBC_ENCODER *encoder, const int16_t *pcm, size_t samples,
                  uint8_t *out, size_t capacity, size_t *written);

#endif /* SONATINA_CODEC_SBC_H */
