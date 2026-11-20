#include "codec_ldac.h"

#ifdef SONATINA_WITH_LIBLDAC
#include <ldacBT.h>

struct SONT_LDAC_ENCODER {
    HANDLE_LDAC_BT handle;
    uint32_t sampling;
    uint8_t channel_mode;
    uint8_t eqmid;
    unsigned channels;
};

static int sont_ldac_to_api_eqmid(uint8_t eqmid)
{
    switch (eqmid) {
    case SONT_LDAC_EQMID_HQ: return LDACBT_EQMID_HQ;
    case SONT_LDAC_EQMID_SQ: return LDACBT_EQMID_SQ;
    case SONT_LDAC_EQMID_MQ: return LDACBT_EQMID_MQ;
    default:                 return -1;
    }
}

SONT_LDAC_ENCODER *SontLdacEncoderCreate(const SONT_LDAC_PARAMS *params, uint16_t mtu)
{
    SONT_LDAC_ENCODER *enc;
    int cm_bit;
    int api_eqmid;

    if (params == NULL || SontLdacSamplingBit(params->sampling) == 0) {
        return NULL;
    }
    cm_bit = (int)SontLdacChannelModeBit(params->channel_mode);
    api_eqmid = sont_ldac_to_api_eqmid(params->eqmid);
    if (cm_bit == 0 || api_eqmid < 0) {
        return NULL;
    }
    if (mtu < 679u) {
        mtu = 679u;
    }

    enc = (SONT_LDAC_ENCODER *)SONT_ALLOC(sizeof(*enc));
    if (enc == NULL) {
        return NULL;
    }
    SONT_MEMSET(enc, 0, sizeof(*enc));
    enc->handle = ldacBT_get_handle();
    if (enc->handle == NULL) {
        SONT_FREE(enc);
        return NULL;
    }
    if (ldacBT_init_handle_encode(enc->handle,
                                  (int)mtu,
                                  api_eqmid,
                                  cm_bit,
                                  LDACBT_SMPL_FMT_S16,
                                  (int)params->sampling) != 0) {
        ldacBT_free_handle(enc->handle);
        SONT_FREE(enc);
        return NULL;
    }
    enc->sampling = params->sampling;
    enc->channel_mode = params->channel_mode;
    enc->eqmid = params->eqmid;
    enc->channels = SontLdacChannels(params);
    return enc;
}

void SontLdacEncoderDestroy(SONT_LDAC_ENCODER *encoder)
{
    if (encoder == NULL) {
        return;
    }
    if (encoder->handle != NULL) {
        ldacBT_close_handle(encoder->handle);
        ldacBT_free_handle(encoder->handle);
        encoder->handle = NULL;
    }
    SONT_FREE(encoder);
}

int SontLdacSetEqmid(SONT_LDAC_ENCODER *encoder, uint8_t eqmid)
{
    int api_eqmid;
    if (encoder == NULL || encoder->handle == NULL) {
        return -1;
    }
    api_eqmid = sont_ldac_to_api_eqmid(eqmid);
    if (api_eqmid < 0) {
        return -1;
    }
    if (ldacBT_set_eqmid(encoder->handle, api_eqmid) != 0) {
        return -1;
    }
    encoder->eqmid = eqmid;
    return 0;
}

uint8_t SontLdacGetEqmid(const SONT_LDAC_ENCODER *encoder)
{
    return (encoder != NULL) ? encoder->eqmid : SONT_LDAC_EQMID_HQ;
}

int SontLdacEncode(SONT_LDAC_ENCODER *encoder,
                   const int16_t *pcm,
                   size_t samples_per_ch,
                   uint8_t *out,
                   size_t capacity,
                   size_t *written,
                   uint8_t *frame_num)
{
    int pcm_used = 0;
    int stream_sz = 0;
    int frames = 0;

    if (encoder == NULL || encoder->handle == NULL || pcm == NULL ||
        out == NULL || written == NULL || frame_num == NULL) {
        return -1;
    }
    *written = 0;
    *frame_num = 0;
    if (samples_per_ch != SONT_LDAC_PCM_SAMPLES_PER_PUSH ||
        capacity < LDACBT_MAX_NBYTES) {
        return -1;
    }

    if (ldacBT_encode(encoder->handle,
                      (void *)pcm,
                      &pcm_used,
                      out,
                      &stream_sz,
                      &frames) != 0) {
        return -1;
    }
    if (stream_sz > 0 && frames > 0) {
        *written = (size_t)stream_sz;
        *frame_num = (uint8_t)((frames > 15) ? 15 : frames);
    }
    return 0;
}

#else /* SONATINA_WITH_LIBLDAC not defined */

struct SONT_LDAC_ENCODER {
    uint8_t eqmid;
};

SONT_LDAC_ENCODER *SontLdacEncoderCreate(const SONT_LDAC_PARAMS *params, uint16_t mtu)
{
    SONT_LDAC_ENCODER *enc;
    (void)mtu;
    if (params == NULL) {
        return NULL;
    }
    enc = (SONT_LDAC_ENCODER *)SONT_ALLOC(sizeof(*enc));
    if (enc != NULL) {
        enc->eqmid = params->eqmid;
    }
    return enc;
}

void SontLdacEncoderDestroy(SONT_LDAC_ENCODER *encoder)
{
    if (encoder != NULL) {
        SONT_FREE(encoder);
    }
}

int SontLdacSetEqmid(SONT_LDAC_ENCODER *encoder, uint8_t eqmid)
{
    if (encoder == NULL || eqmid > SONT_LDAC_EQMID_MQ) {
        return -1;
    }
    encoder->eqmid = eqmid;
    return 0;
}

uint8_t SontLdacGetEqmid(const SONT_LDAC_ENCODER *encoder)
{
    return (encoder != NULL) ? encoder->eqmid : SONT_LDAC_EQMID_HQ;
}

int SontLdacEncode(SONT_LDAC_ENCODER *encoder,
                   const int16_t *pcm,
                   size_t samples_per_ch,
                   uint8_t *out,
                   size_t capacity,
                   size_t *written,
                   uint8_t *frame_num)
{
    (void)encoder;
    (void)pcm;
    (void)samples_per_ch;
    if (out == NULL || capacity < 4 || written == NULL || frame_num == NULL) {
        return -1;
    }
    out[0] = 0xAA;
    out[1] = 0x00;
    *written = 2;
    *frame_num = 1;
    return 0;
}

#endif

unsigned SontLdacChannels(const SONT_LDAC_PARAMS *params)
{
    if (params == NULL) {
        return 2u;
    }
    return (params->channel_mode == SONT_LDAC_CHMODE_MONO) ? 1u : 2u;
}

unsigned SontLdacBitrateKbpsInt(uint32_t sampling, uint8_t eqmid)
{
    int base_44k = (sampling == 44100u || sampling == 88200u);
    switch (eqmid) {
    case SONT_LDAC_EQMID_HQ: return base_44k ? 909u : 990u;
    case SONT_LDAC_EQMID_SQ: return base_44k ? 606u : 660u;
    case SONT_LDAC_EQMID_MQ: return base_44k ? 303u : 330u;
    default:                 return 0u;
    }
}

uint8_t SontLdacSamplingBit(uint32_t sampling)
{
    switch (sampling) {
    case 44100u: return 0x20u;
    case 48000u: return 0x10u;
    case 88200u: return 0x08u;
    case 96000u: return 0x04u;
    default:     return 0u;
    }
}

uint8_t SontLdacChannelModeBit(uint8_t channel_mode)
{
    switch (channel_mode) {
    case SONT_LDAC_CHMODE_MONO:   return 0x04u;
    case SONT_LDAC_CHMODE_DUAL:   return 0x02u;
    case SONT_LDAC_CHMODE_STEREO: return 0x01u;
    default:                      return 0u;
    }
}
