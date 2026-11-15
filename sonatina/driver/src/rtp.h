/* RTP header and Bluetooth audio payload framing, portable C (RFC 3550, the audio profile specification). */
#ifndef SONATINA_RTP_H
#define SONATINA_RTP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define SONT_RTP_HEADER_LEN 12
#define SONT_RTP_PAYLOAD_TYPE 96
#define SONT_RTP_CLOCK 90000u

typedef struct _SONT_RTP_HEADER {
    uint8_t version;        /* 2 */
    bool padding;
    bool extension;
    uint8_t csrc_count;
    bool marker;
    uint8_t payload_type;
    uint16_t sequence;
    uint32_t timestamp;
    uint32_t ssrc;
} SONT_RTP_HEADER;

size_t SontRtpEncodeHeader(const SONT_RTP_HEADER *header, uint8_t out[SONT_RTP_HEADER_LEN]);
bool SontRtpDecodeHeader(const uint8_t *data, size_t length, SONT_RTP_HEADER *header);

/* SBC payload: one octet with the frame count in the high nibble, then the
 * concatenated SBC frames. Returns bytes written or 0 on error. */
size_t SontRtpSbcPayload(const uint8_t *frames, size_t frames_len, uint8_t frame_count,
                         uint8_t *out, size_t capacity);

#endif /* SONATINA_RTP_H */
