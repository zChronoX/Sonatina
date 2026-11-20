/* LDAC parameter model and AOSP libldac (Apache-2.0) encoder wrapper (D7).
 *
 * Compiles with -DSONATINA_WITH_LIBLDAC and -D_32BIT_FIXED_POINT (integer-only
 * DSP path, zero floating-point instructions in kernel mode).
 */
#ifndef SONATINA_CODEC_LDAC_H
#define SONATINA_CODEC_LDAC_H

#include "portable.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SONT_LDAC_PCM_SAMPLES_PER_PUSH 128u

/* Channel mode indices (shared with SONATINA_PROFILE.LdacChannelMode). */
#define SONT_LDAC_CHMODE_MONO   0u
#define SONT_LDAC_CHMODE_DUAL   1u
#define SONT_LDAC_CHMODE_STEREO 2u

/* Encode Quality Mode Index (EQMID). */
#define SONT_LDAC_EQMID_HQ 0u  /* 990 / 909 kbps */
#define SONT_LDAC_EQMID_SQ 1u  /* 660 / 606 kbps */
#define SONT_LDAC_EQMID_MQ 2u  /* 330 / 303 kbps */

typedef struct _SONT_LDAC_PARAMS {
    uint32_t sampling;     /* 44100 / 48000 / 88200 / 96000 */
    uint8_t  channel_mode; /* 0 mono, 1 dual, 2 stereo */
    uint8_t  eqmid;        /* 0 HQ, 1 SQ, 2 MQ */
} SONT_LDAC_PARAMS;

unsigned SontLdacChannels(const SONT_LDAC_PARAMS *params);
unsigned SontLdacBitrateKbpsInt(uint32_t sampling, uint8_t eqmid);
uint8_t  SontLdacSamplingBit(uint32_t sampling);
uint8_t  SontLdacChannelModeBit(uint8_t channel_mode);

typedef struct SONT_LDAC_ENCODER SONT_LDAC_ENCODER;

SONT_LDAC_ENCODER *SontLdacEncoderCreate(const SONT_LDAC_PARAMS *params, uint16_t mtu);
void SontLdacEncoderDestroy(SONT_LDAC_ENCODER *encoder);
int  SontLdacSetEqmid(SONT_LDAC_ENCODER *encoder, uint8_t eqmid);
uint8_t SontLdacGetEqmid(const SONT_LDAC_ENCODER *encoder);

/* Feed 128 interleaved int16 PCM samples per channel.
 * Returns 0 on success; `*written` and `*frame_num` are > 0 when an LDAC
 * transport-frame sequence is emitted, or 0 while libldac buffers another block. */
int SontLdacEncode(SONT_LDAC_ENCODER *encoder,
                   const int16_t *pcm,
                   size_t samples_per_ch,
                   uint8_t *out,
                   size_t capacity,
                   size_t *written,
                   uint8_t *frame_num);

#ifdef __cplusplus
}
#endif

#endif /* SONATINA_CODEC_LDAC_H */
