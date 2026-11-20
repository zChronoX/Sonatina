#include "avdtp.h"

#include "portable.h"

/* Encodes the four SBC "Media Codec Specific Information Elements" octets. */
size_t SontAvdtpEncodeSbcOctets(const SONT_SBC_PARAMS *params, uint8_t out[4]);
size_t SontAvdtpEncodeLdacOctets(const SONT_LDAC_PARAMS *params, uint8_t out[8]);

size_t SontAvdtpEncode(uint8_t transaction, uint8_t packet_type, uint8_t message_type,
                       uint8_t signal, const uint8_t *payload, size_t payload_len,
                       uint8_t *out, size_t capacity)
{
    if (out == NULL || capacity < SONT_AVDTP_HEADER_LEN + payload_len) {
        return 0;
    }
    out[0] = (uint8_t)(((transaction & 0x0F) << 4) |
                       ((packet_type & 0x03) << 2) |
                       (message_type & 0x03));
    out[1] = (uint8_t)(signal & 0x3F); /* RFA in bits 7..6, signal ID in 5..0 */
    if (payload_len != 0 && payload != NULL) {
        SONT_MEMCPY(out + SONT_AVDTP_HEADER_LEN, payload, payload_len);
    }
    return SONT_AVDTP_HEADER_LEN + payload_len;
}

bool SontAvdtpDecode(const uint8_t *data, size_t length, SONT_AVDTP_MESSAGE *message)
{
    if (data == NULL || message == NULL || length < SONT_AVDTP_HEADER_LEN) {
        return false;
    }
    /* RFA bits (7..6 of octet 1) are ignored on receive, as the spec requires. */
    message->transaction = (uint8_t)((data[0] >> 4) & 0x0F);
    message->packet_type = (uint8_t)((data[0] >> 2) & 0x03);
    message->message_type = (uint8_t)(data[0] & 0x03);
    message->signal = (uint8_t)(data[1] & 0x3F);
    message->payload = data + SONT_AVDTP_HEADER_LEN;
    message->payload_len = length - SONT_AVDTP_HEADER_LEN;
    return true;
}

static size_t SontAppendCapability(uint8_t *out, size_t offset, size_t capacity,
                                   uint8_t category, const uint8_t *body, size_t body_len)
{
    if (offset + 2 + body_len > capacity) {
        return 0;
    }
    out[offset++] = category;
    out[offset++] = (uint8_t)body_len;
    if (body_len != 0) {
        SONT_MEMCPY(out + offset, body, body_len);
    }
    return offset + body_len;
}

size_t SontAvdtpEncodeCapabilities(const SONT_CAPABILITIES *caps,
                                   uint8_t *out, size_t capacity)
{
    uint8_t header[2];
    uint8_t sbc[4];
    size_t offset;

    if (caps == NULL || out == NULL) {
        return 0;
    }
    offset = SontAppendCapability(out, 0, capacity,
                                  SONT_AVDTP_CAT_MEDIA_TRANSPORT, NULL, 0);
    if (offset == 0) {
        return 0;
    }
    header[0] = 0x00; /* media type audio in the high nibble, RFA in the low */
    header[1] = caps->codec;
    if (caps->codec == SONT_AVDTP_CODEC_SBC) {
        if (SontAvdtpEncodeSbcOctets(&caps->sbc, sbc) != 4) {
            return 0;
        }
        if (offset + 2 + 2 + 4 > capacity) {
            return 0;
        }
        out[offset++] = SONT_AVDTP_CAT_MEDIA_CODEC;
        out[offset++] = 6;
        out[offset++] = header[0];
        out[offset++] = header[1];
        SONT_MEMCPY(out + offset, sbc, 4);
        return offset + 4;
    }
    if (caps->codec == SONT_AVDTP_CODEC_LDAC) {
        uint8_t ldac[8];
        if (SontAvdtpEncodeLdacOctets(&caps->ldac, ldac) != 8) {
            return 0;
        }
        if (offset + 2 + 2 + 8 > capacity) {
            return 0;
        }
        out[offset++] = SONT_AVDTP_CAT_MEDIA_CODEC;
        out[offset++] = 10;
        out[offset++] = header[0];
        out[offset++] = SONT_AVDTP_CODEC_RAW; /* 0xFF vendor-specific on wire */
        SONT_MEMCPY(out + offset, ldac, 8);
        return offset + 8;
    }
    if (offset + 2 + 2 > capacity) {
        return 0;
    }
    out[offset++] = SONT_AVDTP_CAT_MEDIA_CODEC;
    out[offset++] = 2;
    out[offset++] = header[0];
    out[offset++] = header[1];
    return offset;
}

size_t SontAvdtpEncodeSbcOctets(const SONT_SBC_PARAMS *params, uint8_t out[4])
{
    static const uint8_t freq_bits[] = {0x80, 0x40, 0x20, 0x10};   /* 16/32/44.1/48 */
    static const uint8_t mode_bits[] = {0x08, 0x04, 0x02, 0x01};
    static const uint8_t block_bits[] = {0x80, 0x40, 0x20, 0x10};  /* 4/8/12/16 */
    int freq_index = -1, block_index = -1;

    if (params == NULL || out == NULL) {
        return 0;
    }
    switch (params->sampling) {
    case 16000: freq_index = 0; break;
    case 32000: freq_index = 1; break;
    case 44100: freq_index = 2; break;
    case 48000: freq_index = 3; break;
    default: break;
    }
    switch (params->blocks) {
    case 4:  block_index = 0; break;
    case 8:  block_index = 1; break;
    case 12: block_index = 2; break;
    case 16: block_index = 3; break;
    default: break;
    }
    if (freq_index < 0 || block_index < 0 || params->channel_mode > 3) {
        return 0;
    }
    if ((params->subbands != 4 && params->subbands != 8) || params->allocation > 1) {
        return 0;
    }
    if (params->min_bitpool < 2 || params->max_bitpool < params->min_bitpool ||
        params->max_bitpool > 250) {
        return 0;
    }
    out[0] = (uint8_t)(freq_bits[freq_index] | mode_bits[params->channel_mode]);
    out[1] = (uint8_t)(block_bits[block_index] |
                       ((params->subbands == 8) ? 0x04 : 0x08) |
                       ((params->allocation == 0) ? 0x02 : 0x01));
    out[2] = params->min_bitpool;
    out[3] = params->max_bitpool;
    return 4;
}

static bool SontDecodeSbcOctets(const uint8_t in[4], SONT_SBC_PARAMS *params)
{
    static const uint32_t freqs[] = {16000, 32000, 44100, 48000};
    static const uint8_t blocks[] = {4, 8, 12, 16};
    int i, found;

    SONT_MEMSET(params, 0, sizeof(*params));
    found = 0;
    for (i = 0; i < 4; i++) {
        if (in[0] & (0x80 >> i)) { params->sampling = freqs[i]; found = 1; }
    }
    if (!found) {
        return false;
    }
    params->channel_mode = (uint8_t)((in[0] & 0x01) ? 3 :
                                     (in[0] & 0x02) ? 2 :
                                     (in[0] & 0x04) ? 1 : 0);
    found = 0;
    for (i = 0; i < 4; i++) {
        if (in[1] & (0x80 >> i)) { params->blocks = blocks[i]; found = 1; }
    }
    if (!found) {
        return false;
    }
    params->subbands = (in[1] & 0x04) ? 8 : 4;
    params->allocation = (in[1] & 0x02) ? 0 : 1;
    params->min_bitpool = in[2];
    params->max_bitpool = in[3];
    if (params->min_bitpool > params->max_bitpool) {
        uint8_t tmp = params->min_bitpool;
        params->min_bitpool = params->max_bitpool;
        params->max_bitpool = tmp;
    }
    return true;
}

size_t SontAvdtpEncodeLdacOctets(const SONT_LDAC_PARAMS *params, uint8_t out[8])
{
    uint8_t sf_bit;
    uint8_t cm_bit;

    if (params == NULL || out == NULL) {
        return 0;
    }
    sf_bit = SontLdacSamplingBit(params->sampling);
    cm_bit = SontLdacChannelModeBit(params->channel_mode);
    if (sf_bit == 0 || cm_bit == 0) {
        return 0;
    }
    out[0] = 0x2Du;
    out[1] = 0x01u;
    out[2] = 0x00u;
    out[3] = 0x00u;
    out[4] = 0xAAu;
    out[5] = 0x00u;
    out[6] = sf_bit;
    out[7] = cm_bit;
    return 8;
}

static bool SontDecodeLdacOctets(const uint8_t in[8], SONT_LDAC_PARAMS *params)
{
    static const uint32_t freqs[] = {96000u, 88200u, 48000u, 44100u};
    static const uint8_t fbits[] = {0x04u, 0x08u, 0x10u, 0x20u};
    size_t i;
    bool ok = false;

    if (in[0] != 0x2Du || in[1] != 0x01u || in[2] != 0x00u || in[3] != 0x00u ||
        in[4] != 0xAAu || in[5] != 0x00u) {
        return false;
    }
    SONT_MEMSET(params, 0, sizeof(*params));
    for (i = 0; i < 4; i++) {
        if ((in[6] & fbits[i]) != 0) {
            params->sampling = freqs[i];
            ok = true;
            break;
        }
    }
    if (!ok) {
        return false;
    }
    if ((in[7] & 0x01u) != 0) {
        params->channel_mode = SONT_LDAC_CHMODE_STEREO;
    } else if ((in[7] & 0x02u) != 0) {
        params->channel_mode = SONT_LDAC_CHMODE_DUAL;
    } else if ((in[7] & 0x04u) != 0) {
        params->channel_mode = SONT_LDAC_CHMODE_MONO;
    } else {
        return false;
    }
    params->eqmid = SONT_LDAC_EQMID_HQ;
    return true;
}

bool SontAvdtpParseCapabilities(const uint8_t *data, size_t length,
                                SONT_CAPABILITIES *caps)
{
    size_t offset = 0;
    bool seen_codec = false;

    if (data == NULL || caps == NULL) {
        return false;
    }
    SONT_MEMSET(caps, 0, sizeof(*caps));
    while (offset < length) {
        uint8_t category, body_len;
        const uint8_t *body;

        if (offset + 2 > length) {
            return false;
        }
        category = data[offset];
        body_len = data[offset + 1];
        offset += 2;
        if (offset + body_len > length) {
            return false;
        }
        body = data + offset;
        offset += body_len;
        if (caps->category_count < sizeof(caps->categories)) {
            caps->categories[caps->category_count++] = category;
        }
        if (category == SONT_AVDTP_CAT_MEDIA_TRANSPORT && body_len != 0) {
            return false;
        }
        if (category == SONT_AVDTP_CAT_MEDIA_CODEC) {
            if (seen_codec || body_len < 2) {
                return false;
            }
            if (((body[0] >> 4) & 0x0F) != 0) {
                return false;
            }
            seen_codec = true;
            caps->codec = body[1];
            if (caps->codec == SONT_AVDTP_CODEC_SBC) {
                if (body_len < 6 || !SontDecodeSbcOctets(body + 2, &caps->sbc)) {
                    return false;
                }
                SONT_MEMCPY(caps->raw_sbc, body + 2, 4);
                caps->has_raw_sbc = true;
            } else if (caps->codec == SONT_AVDTP_CODEC_RAW && body_len >= 10 &&
                       body[2] == 0x2Du && body[3] == 0x01u &&
                       body[4] == 0x00u && body[5] == 0x00u &&
                       body[6] == 0xAAu && body[7] == 0x00u) {
                if (!SontDecodeLdacOctets(body + 2, &caps->ldac)) {
                    return false;
                }
                caps->codec = SONT_AVDTP_CODEC_LDAC;
                SONT_MEMCPY(caps->raw_ldac, body + 2, 8);
                caps->has_raw_ldac = true;
            }
        }
    }
    return seen_codec;
}

size_t SontAvdtpEncodeSingleSeid(uint8_t seid, uint8_t *out, size_t capacity)
{
    if (out == NULL || capacity < 1 || seid == 0 || seid > 62) {
        return 0;
    }
    out[0] = (uint8_t)((seid & 0x3F) << 2); /* SEID in bits 7..2, RFA in 1..0 */
    return 1;
}

size_t SontAvdtpEncodeSepList(const SONT_SEP_INFO *seps, size_t count,
                              uint8_t *out, size_t capacity)
{
    size_t i;

    if (out == NULL || count == 0 || seps == NULL || capacity < count * 2) {
        return 0;
    }
    for (i = 0; i < count; i++) {
        if (seps[i].seid == 0 || seps[i].seid > 62) {
            return 0;
        }
        /* Octet 0: SEID (7..2), In Use (1), RFA (0).
         * Octet 1: Media Type (7..4), TSEP (3), RFA (2..0). */
        out[i * 2] = (uint8_t)(((seps[i].seid & 0x3F) << 2) |
                               (seps[i].in_use ? 0x02 : 0x00));
        out[i * 2 + 1] = (uint8_t)(((seps[i].media_type & 0x0F) << 4) |
                                   ((seps[i].tsep & 0x01) << 3));
    }
    return count * 2;
}

bool SontAvdtpParseSepList(const uint8_t *data, size_t length,
                           SONT_SEP_INFO *seps, size_t max_seps, size_t *out_count)
{
    size_t i;
    size_t count = 0;

    if (seps == NULL || out_count == NULL || (length != 0 && data == NULL) ||
        (length % 2) != 0) {
        return false;
    }
    for (i = 0; i < length; i += 2) {
        uint8_t seid = (uint8_t)((data[i] >> 2) & 0x3F);
        if (seid == 0) {
            return false;
        }
        if (count < max_seps) {
            seps[count].seid = seid;
            seps[count].in_use = (data[i] & 0x02) != 0;
            seps[count].media_type = (uint8_t)((data[i + 1] >> 4) & 0x0F);
            seps[count].tsep = (uint8_t)((data[i + 1] >> 3) & 0x01);
            count++;
        }
    }
    *out_count = count;
    return true;
}

bool SontAvdtpSelectSbcConfig(const uint8_t peer_sbc[4],
                              const SONT_SBC_PARAMS *preferred,
                              SONT_SBC_PARAMS *selected)
{
    static const uint32_t freqs[] = {48000, 44100, 32000, 16000};
    static const uint8_t freq_bits[] = {0x10, 0x20, 0x40, 0x80};
    static const uint8_t modes[] = {3, 2, 1, 0};
    static const uint8_t mode_bits[] = {0x01, 0x02, 0x04, 0x08};
    static const uint8_t blocks[] = {16, 12, 8, 4};
    static const uint8_t block_bits[] = {0x10, 0x20, 0x40, 0x80};
    SONT_SBC_PARAMS result;
    uint8_t peer_min, peer_max, min_bp, max_bp;
    size_t i;
    bool ok;

    if (peer_sbc == NULL || preferred == NULL || selected == NULL) {
        return false;
    }
    SONT_MEMSET(&result, 0, sizeof(result));

    /* Sampling frequency: prefer requested rate if sink advertises it, else highest supported. */
    ok = false;
    for (i = 0; i < 4; i++) {
        if (preferred->sampling == freqs[i] && (peer_sbc[0] & freq_bits[i]) != 0) {
            result.sampling = freqs[i];
            ok = true;
            break;
        }
    }
    if (!ok) {
        for (i = 0; i < 4; i++) {
            if ((peer_sbc[0] & freq_bits[i]) != 0) {
                result.sampling = freqs[i];
                ok = true;
                break;
            }
        }
    }
    if (!ok) {
        return false;
    }

    /* Channel mode: prefer requested mode, else joint -> stereo -> dual -> mono. */
    ok = false;
    for (i = 0; i < 4; i++) {
        if (preferred->channel_mode == modes[i] && (peer_sbc[0] & mode_bits[i]) != 0) {
            result.channel_mode = modes[i];
            ok = true;
            break;
        }
    }
    if (!ok) {
        for (i = 0; i < 4; i++) {
            if ((peer_sbc[0] & mode_bits[i]) != 0) {
                result.channel_mode = modes[i];
                ok = true;
                break;
            }
        }
    }
    if (!ok) {
        return false;
    }

    /* Block length: prefer requested, else 16 -> 12 -> 8 -> 4. */
    ok = false;
    for (i = 0; i < 4; i++) {
        if (preferred->blocks == blocks[i] && (peer_sbc[1] & block_bits[i]) != 0) {
            result.blocks = blocks[i];
            ok = true;
            break;
        }
    }
    if (!ok) {
        for (i = 0; i < 4; i++) {
            if ((peer_sbc[1] & block_bits[i]) != 0) {
                result.blocks = blocks[i];
                ok = true;
                break;
            }
        }
    }
    if (!ok) {
        return false;
    }

    /* Subbands: bit 2 = 8 subbands, bit 3 = 4 subbands. */
    if ((peer_sbc[1] & 0x0C) == 0) {
        return false;
    }
    if (preferred->subbands == 8 && (peer_sbc[1] & 0x04) != 0) {
        result.subbands = 8;
    } else if (preferred->subbands == 4 && (peer_sbc[1] & 0x08) != 0) {
        result.subbands = 4;
    } else {
        result.subbands = (peer_sbc[1] & 0x04) ? 8 : 4;
    }

    /* Allocation: bit 0 = loudness (1), bit 1 = SNR (0). */
    if ((peer_sbc[1] & 0x03) == 0) {
        return false;
    }
    if (preferred->allocation == 1 && (peer_sbc[1] & 0x01) != 0) {
        result.allocation = 1;
    } else if (preferred->allocation == 0 && (peer_sbc[1] & 0x02) != 0) {
        result.allocation = 0;
    } else {
        result.allocation = (peer_sbc[1] & 0x01) ? 1 : 0;
    }

    peer_min = peer_sbc[2];
    peer_max = peer_sbc[3];
    if (peer_min > peer_max) {
        uint8_t tmp = peer_min;
        peer_min = peer_max;
        peer_max = tmp;
    }
    min_bp = (preferred->min_bitpool > peer_min) ? preferred->min_bitpool : peer_min;
    max_bp = (preferred->max_bitpool < peer_max) ? preferred->max_bitpool : peer_max;
    if (min_bp < 2) {
        min_bp = 2;
    }
    if (min_bp > max_bp) {
        return false;
    }
    result.min_bitpool = min_bp;
    result.max_bitpool = max_bp;
    *selected = result;
    return true;
}

bool SontAvdtpSelectLdacConfig(const uint8_t peer_ldac[8],
                               const SONT_LDAC_PARAMS *preferred,
                               SONT_LDAC_PARAMS *selected)
{
    static const uint32_t freqs[] = {96000u, 88200u, 48000u, 44100u};
    static const uint8_t fbits[] = {0x04u, 0x08u, 0x10u, 0x20u};
    static const uint8_t modes[] = {SONT_LDAC_CHMODE_STEREO, SONT_LDAC_CHMODE_DUAL, SONT_LDAC_CHMODE_MONO};
    static const uint8_t mbits[] = {0x01u, 0x02u, 0x04u};
    SONT_LDAC_PARAMS result;
    size_t i;
    bool ok;

    if (peer_ldac == NULL || preferred == NULL || selected == NULL) {
        return false;
    }
    if (peer_ldac[0] != 0x2Du || peer_ldac[1] != 0x01u ||
        peer_ldac[2] != 0x00u || peer_ldac[3] != 0x00u ||
        peer_ldac[4] != 0xAAu || peer_ldac[5] != 0x00u) {
        return false;
    }

    SONT_MEMSET(&result, 0, sizeof(result));
    ok = false;
    for (i = 0; i < 4; i++) {
        if (preferred->sampling == freqs[i] && (peer_ldac[6] & fbits[i]) != 0) {
            result.sampling = freqs[i];
            ok = true;
            break;
        }
    }
    if (!ok) {
        for (i = 0; i < 4; i++) {
            if ((peer_ldac[6] & fbits[i]) != 0) {
                result.sampling = freqs[i];
                ok = true;
                break;
            }
        }
    }
    if (!ok) {
        return false;
    }

    ok = false;
    for (i = 0; i < 3; i++) {
        if (preferred->channel_mode == modes[i] && (peer_ldac[7] & mbits[i]) != 0) {
            result.channel_mode = modes[i];
            ok = true;
            break;
        }
    }
    if (!ok) {
        for (i = 0; i < 3; i++) {
            if ((peer_ldac[7] & mbits[i]) != 0) {
                result.channel_mode = modes[i];
                ok = true;
                break;
            }
        }
    }
    if (!ok) {
        return false;
    }

    result.eqmid = (preferred->eqmid <= SONT_LDAC_EQMID_MQ) ? preferred->eqmid : SONT_LDAC_EQMID_HQ;
    *selected = result;
    return true;
}

size_t SontAvdtpEncodeSetConfiguration(uint8_t acp_seid, uint8_t int_seid,
                                       const SONT_CAPABILITIES *caps,
                                       uint8_t *out, size_t capacity)
{
    size_t written;

    if (out == NULL || capacity < 2) {
        return 0;
    }
    if (acp_seid == 0 || acp_seid > 62 || int_seid == 0 || int_seid > 62) {
        return 0;
    }
    out[0] = (uint8_t)((acp_seid & 0x3F) << 2);
    out[1] = (uint8_t)((int_seid & 0x3F) << 2);
    written = SontAvdtpEncodeCapabilities(caps, out + 2, capacity - 2);
    if (written == 0) {
        return 0;
    }
    return written + 2;
}

/* ------------------------------------------------------------------ source SEP */

size_t SontAvdtpEncodeSourceSbcCapabilities(uint8_t max_bitpool,
                                            uint8_t *out, size_t capacity)
{
    size_t offset;

    if (out == NULL || capacity < 2 + 2 + 2 + 4) {
        return 0;
    }
    if (max_bitpool < 2) {
        max_bitpool = 2;
    }
    offset = SontAppendCapability(out, 0, capacity, SONT_AVDTP_CAT_MEDIA_TRANSPORT, NULL, 0);
    out[offset++] = SONT_AVDTP_CAT_MEDIA_CODEC;
    out[offset++] = 6;
    out[offset++] = 0x00;                 /* audio */
    out[offset++] = SONT_AVDTP_CODEC_SBC;
    out[offset++] = 0x20 | 0x10 | 0x0F;   /* 44.1/48 kHz, all channel modes */
    out[offset++] = 0xF0 | 0x0C | 0x03;   /* 4..16 blocks, 4/8 subbands, SNR+loudness */
    out[offset++] = 2;
    out[offset++] = max_bitpool;
    return offset;
}

size_t SontAvdtpEncodeSourceLdacCapabilities(uint8_t ldac_sampling_mask,
                                             uint8_t *out, size_t capacity)
{
    size_t offset;

    ldac_sampling_mask &= 0x3C; /* 44.1/48/88.2/96 kHz */
    if (out == NULL || capacity < 2 + 2 + 2 + 8 || ldac_sampling_mask == 0) {
        return 0;
    }
    offset = SontAppendCapability(out, 0, capacity, SONT_AVDTP_CAT_MEDIA_TRANSPORT, NULL, 0);
    out[offset++] = SONT_AVDTP_CAT_MEDIA_CODEC;
    out[offset++] = 10;
    out[offset++] = 0x00;                 /* audio */
    out[offset++] = SONT_AVDTP_CODEC_RAW; /* vendor specific */
    out[offset++] = 0x2Du;
    out[offset++] = 0x01u;
    out[offset++] = 0x00u;
    out[offset++] = 0x00u;
    out[offset++] = 0xAAu;
    out[offset++] = 0x00u;
    out[offset++] = ldac_sampling_mask;
    out[offset++] = 0x07u;                /* mono, dual, stereo */
    return offset;
}

static bool SontIsSingleBit(uint8_t v)
{
    return v != 0 && (v & (uint8_t)(v - 1u)) == 0;
}

bool SontAvdtpSourceAcceptsConfig(const SONT_CAPABILITIES *cfg, uint8_t max_bitpool,
                                  uint8_t ldac_sampling_mask)
{
    if (cfg == NULL) {
        return false;
    }
    if (cfg->codec == SONT_AVDTP_CODEC_SBC) {
        const uint8_t *o = cfg->raw_sbc;
        if (!cfg->has_raw_sbc) {
            return false;
        }
        /* Exactly one choice per field, inside what the source advertises. */
        if (!SontIsSingleBit((uint8_t)(o[0] & 0xF0)) || (o[0] & 0xC0) != 0 ||
            !SontIsSingleBit((uint8_t)(o[0] & 0x0F)) ||
            !SontIsSingleBit((uint8_t)(o[1] & 0xF0)) ||
            !SontIsSingleBit((uint8_t)(o[1] & 0x0C)) ||
            !SontIsSingleBit((uint8_t)(o[1] & 0x03))) {
            return false;
        }
        return cfg->sbc.min_bitpool >= 2 && cfg->sbc.max_bitpool <= max_bitpool &&
               cfg->sbc.min_bitpool <= cfg->sbc.max_bitpool;
    }
    if (cfg->codec == SONT_AVDTP_CODEC_LDAC) {
        const uint8_t *o = cfg->raw_ldac;
        if (!cfg->has_raw_ldac) {
            return false;
        }
        return SontIsSingleBit(o[6]) && (o[6] & ldac_sampling_mask & 0x3C) != 0 &&
               SontIsSingleBit((uint8_t)(o[7] & 0x07));
    }
    return false;
}
