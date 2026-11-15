#include "rtp.h"

#include "portable.h"

static uint16_t SontReadBe16(const uint8_t *p)
{
    return (uint16_t)(((uint16_t)p[0] << 8) | p[1]);
}

static uint32_t SontReadBe32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

size_t SontRtpEncodeHeader(const SONT_RTP_HEADER *header, uint8_t out[SONT_RTP_HEADER_LEN])
{
    if (header == NULL || out == NULL) {
        return 0;
    }
    out[0] = (uint8_t)(((header->version & 0x03) << 6) |
                       (header->padding ? 0x20 : 0) |
                       (header->extension ? 0x10 : 0) |
                       (header->csrc_count & 0x0F));
    out[1] = (uint8_t)((header->marker ? 0x80 : 0) | (header->payload_type & 0x7F));
    out[2] = (uint8_t)(header->sequence >> 8);
    out[3] = (uint8_t)(header->sequence & 0xFF);
    out[4] = (uint8_t)(header->timestamp >> 24);
    out[5] = (uint8_t)(header->timestamp >> 16);
    out[6] = (uint8_t)(header->timestamp >> 8);
    out[7] = (uint8_t)(header->timestamp & 0xFF);
    out[8] = (uint8_t)(header->ssrc >> 24);
    out[9] = (uint8_t)(header->ssrc >> 16);
    out[10] = (uint8_t)(header->ssrc >> 8);
    out[11] = (uint8_t)(header->ssrc & 0xFF);
    return SONT_RTP_HEADER_LEN;
}

bool SontRtpDecodeHeader(const uint8_t *data, size_t length, SONT_RTP_HEADER *header)
{
    if (data == NULL || header == NULL || length < SONT_RTP_HEADER_LEN) {
        return false;
    }
    if (((data[0] >> 6) & 0x03) != 2) {
        return false;
    }
    header->version = 2;
    header->padding = (data[0] & 0x20) != 0;
    header->extension = (data[0] & 0x10) != 0;
    header->csrc_count = (uint8_t)(data[0] & 0x0F);
    header->marker = (data[1] & 0x80) != 0;
    header->payload_type = (uint8_t)(data[1] & 0x7F);
    header->sequence = SontReadBe16(data + 2);
    header->timestamp = SontReadBe32(data + 4);
    header->ssrc = SontReadBe32(data + 8);
    if (length < SONT_RTP_HEADER_LEN + (size_t)header->csrc_count * 4u) {
        return false;
    }
    return true;
}

size_t SontRtpSbcPayload(const uint8_t *frames, size_t frames_len, uint8_t frame_count,
                         uint8_t *out, size_t capacity)
{
    if (out == NULL || frame_count == 0 || frame_count > 15 || capacity < 1 + frames_len) {
        return 0;
    }
    if (frames_len != 0 && frames == NULL) {
        return 0;
    }
    out[0] = (uint8_t)((frame_count & 0x0F) << 4);
    if (frames_len != 0) {
        SONT_MEMCPY(out + 1, frames, frames_len);
    }
    return frames_len + 1;
}
