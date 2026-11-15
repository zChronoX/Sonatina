#include "avdtp.h"

#include "portable.h"

/* Encodes the four SBC "Media Codec Specific Information Elements" octets. */
size_t SontAvdtpEncodeSbcOctets(const SONT_SBC_PARAMS *params, uint8_t out[4]);

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
    out[1] = (uint8_t)((signal & 0x3F) << 2);
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
    if ((data[1] & 0x03) != 0) {
        return false; /* RFA bits must be zero */
    }
    message->transaction = (uint8_t)((data[0] >> 4) & 0x0F);
    message->packet_type = (uint8_t)((data[0] >> 2) & 0x03);
    message->message_type = (uint8_t)(data[0] & 0x03);
    message->signal = (uint8_t)((data[1] >> 2) & 0x3F);
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
            }
        }
    }
    return seen_codec;
}

size_t SontAvdtpEncodeSetConfiguration(uint8_t acp_seid, uint8_t int_seid,
                                       const SONT_CAPABILITIES *caps,
                                       uint8_t *out, size_t capacity)
{
    size_t written;

    if (out == NULL || capacity < 2) {
        return 0;
    }
    out[0] = (uint8_t)(acp_seid & 0x3F);
    out[1] = (uint8_t)(int_seid & 0x3F);
    written = SontAvdtpEncodeCapabilities(caps, out + 2, capacity - 2);
    if (written == 0) {
        return 0;
    }
    return written + 2;
}
