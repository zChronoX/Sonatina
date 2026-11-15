/* AVDTP signalling subset (the audio profile specification, section 8), portable C.
 *
 * This mirrors sonatina/avdtp.py so the driver and the Python reference produce
 * identical bytes on the wire.
 */
#ifndef SONATINA_AVDTP_H
#define SONATINA_AVDTP_H

#include "portable.h"

#include "codec_sbc.h"

#define SONT_AVDTP_HEADER_LEN 2

/* Packet types (2 bits). */
#define SONT_AVDTP_PT_SINGLE 0

/* Message types (2 bits). */
#define SONT_AVDTP_MT_COMMAND 0
#define SONT_AVDTP_MT_ACCEPT  2
#define SONT_AVDTP_MT_REJECT  3

/* Signal identifiers. */
#define SONT_AVDTP_SIG_DISCOVER           0x01
#define SONT_AVDTP_SIG_GET_CAPABILITIES   0x02
#define SONT_AVDTP_SIG_SET_CONFIGURATION  0x03
#define SONT_AVDTP_SIG_OPEN               0x06
#define SONT_AVDTP_SIG_START              0x07
#define SONT_AVDTP_SIG_CLOSE              0x08
#define SONT_AVDTP_SIG_SUSPEND            0x09
#define SONT_AVDTP_SIG_ABORT              0x0A

/* Service categories. */
#define SONT_AVDTP_CAT_MEDIA_TRANSPORT 1
#define SONT_AVDTP_CAT_MEDIA_CODEC     7

/* Codec types inside the Media Codec capability. */
#define SONT_AVDTP_CODEC_SBC  0x00
#define SONT_AVDTP_CODEC_AAC  0x02
#define SONT_AVDTP_CODEC_RAW  0xFF  /* non-Bluetooth audio: raw PCM or aptX */

typedef struct _SONT_AVDTP_MESSAGE {
    uint8_t transaction;
    uint8_t packet_type;
    uint8_t message_type;
    uint8_t signal;
    const uint8_t *payload;
    size_t payload_len;
} SONT_AVDTP_MESSAGE;

typedef struct _SONT_CAPABILITIES {
    uint8_t codec;              /* SONT_AVDTP_CODEC_* */
    uint8_t categories[8];
    uint8_t category_count;
    SONT_SBC_PARAMS sbc;
} SONT_CAPABILITIES;

size_t SontAvdtpEncode(uint8_t transaction, uint8_t packet_type, uint8_t message_type,
                       uint8_t signal, const uint8_t *payload, size_t payload_len,
                       uint8_t *out, size_t capacity);

bool SontAvdtpDecode(const uint8_t *data, size_t length, SONT_AVDTP_MESSAGE *message);

size_t SontAvdtpEncodeCapabilities(const SONT_CAPABILITIES *caps,
                                   uint8_t *out, size_t capacity);

bool SontAvdtpParseCapabilities(const uint8_t *data, size_t length,
                                SONT_CAPABILITIES *caps);

size_t SontAvdtpEncodeSetConfiguration(uint8_t acp_seid, uint8_t int_seid,
                                       const SONT_CAPABILITIES *caps,
                                       uint8_t *out, size_t capacity);

#endif /* SONATINA_AVDTP_H */
