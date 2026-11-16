/* AVDTP signalling subset (the audio profile specification, section 8), portable C.
 *
 * This mirrors sonatina/avdtp.py so the driver and the Python reference produce
 * identical bytes on the wire.
 */
#ifndef SONATINA_AVDTP_H
#define SONATINA_AVDTP_H

#include "portable.h"

#include "codec_sbc.h"
#include "codec_ldac.h"

#define SONT_AVDTP_HEADER_LEN 2

/* Packet types (2 bits). */
#define SONT_AVDTP_PT_SINGLE 0

/* Message types (2 bits). */
#define SONT_AVDTP_MT_COMMAND        0
#define SONT_AVDTP_MT_GENERAL_REJECT 1
#define SONT_AVDTP_MT_ACCEPT         2
#define SONT_AVDTP_MT_REJECT         3

/* Signal identifiers. */
#define SONT_AVDTP_SIG_DISCOVER           0x01
#define SONT_AVDTP_SIG_GET_CAPABILITIES   0x02
#define SONT_AVDTP_SIG_SET_CONFIGURATION  0x03
#define SONT_AVDTP_SIG_OPEN               0x06
#define SONT_AVDTP_SIG_START              0x07
#define SONT_AVDTP_SIG_CLOSE              0x08
#define SONT_AVDTP_SIG_SUSPEND            0x09
#define SONT_AVDTP_SIG_ABORT              0x0A

/* Error codes returned in Reject responses. */
#define SONT_AVDTP_ERR_BAD_HEADER_FORMAT    0x01
#define SONT_AVDTP_ERR_BAD_LENGTH           0x11
#define SONT_AVDTP_ERR_BAD_ACP_SEID         0x12
#define SONT_AVDTP_ERR_SEP_IN_USE           0x13
#define SONT_AVDTP_ERR_SEP_NOT_IN_USE       0x14
#define SONT_AVDTP_ERR_BAD_SERVICE_CATEGORY 0x19
#define SONT_AVDTP_ERR_INVALID_CAPABILITIES 0x1A

/* Stream End Point discovery constants. */
#define SONT_AVDTP_MEDIA_AUDIO 0
#define SONT_AVDTP_TSEP_SRC    0
#define SONT_AVDTP_TSEP_SNK    1

/* Service categories. */
#define SONT_AVDTP_CAT_MEDIA_TRANSPORT 1
#define SONT_AVDTP_CAT_MEDIA_CODEC     7

/* Codec types inside the Media Codec capability. */
#define SONT_AVDTP_CODEC_SBC  0x00
#define SONT_AVDTP_CODEC_AAC  0x02
#define SONT_AVDTP_CODEC_LDAC 0xFE  /* vendor 0xFF + Sony 0x0000012D / 0x00AA */
#define SONT_AVDTP_CODEC_RAW  0xFF  /* non-Bluetooth audio: raw PCM or aptX */

typedef struct _SONT_SEP_INFO {
    uint8_t seid;       /* 1..62 */
    bool    in_use;
    uint8_t media_type; /* SONT_AVDTP_MEDIA_* */
    uint8_t tsep;       /* SONT_AVDTP_TSEP_* */
} SONT_SEP_INFO;

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
    uint8_t raw_sbc[4];         /* raw SBC bitmask octets when parsed */
    bool    has_raw_sbc;
    SONT_LDAC_PARAMS ldac;
    uint8_t raw_ldac[8];        /* vendor ID + codec ID + sf + cm */
    bool    has_raw_ldac;
} SONT_CAPABILITIES;

size_t SontAvdtpEncode(uint8_t transaction, uint8_t packet_type, uint8_t message_type,
                       uint8_t signal, const uint8_t *payload, size_t payload_len,
                       uint8_t *out, size_t capacity);

bool SontAvdtpDecode(const uint8_t *data, size_t length, SONT_AVDTP_MESSAGE *message);

size_t SontAvdtpEncodeSingleSeid(uint8_t seid, uint8_t *out, size_t capacity);

size_t SontAvdtpEncodeSepList(const SONT_SEP_INFO *seps, size_t count,
                              uint8_t *out, size_t capacity);

bool SontAvdtpParseSepList(const uint8_t *data, size_t length,
                           SONT_SEP_INFO *seps, size_t max_seps, size_t *out_count);

bool SontAvdtpSelectSbcConfig(const uint8_t peer_sbc[4],
                              const SONT_SBC_PARAMS *preferred,
                              SONT_SBC_PARAMS *selected);

bool SontAvdtpSelectLdacConfig(const uint8_t peer_ldac[8],
                               const SONT_LDAC_PARAMS *preferred,
                               SONT_LDAC_PARAMS *selected);

size_t SontAvdtpEncodeCapabilities(const SONT_CAPABILITIES *caps,
                                   uint8_t *out, size_t capacity);

bool SontAvdtpParseCapabilities(const uint8_t *data, size_t length,
                                SONT_CAPABILITIES *caps);

size_t SontAvdtpEncodeSetConfiguration(uint8_t acp_seid, uint8_t int_seid,
                                       const SONT_CAPABILITIES *caps,
                                       uint8_t *out, size_t capacity);

#endif /* SONATINA_AVDTP_H */
