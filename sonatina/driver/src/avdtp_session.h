/* Portable AVDTP initiator session / handshake state machine.
 *
 * Drives Discover -> GetCapabilities -> SetConfiguration -> Open -> Start
 * (plus Suspend / Resume / Close / Abort) using pure memory buffers so it runs
 * identically in host unit tests and in the kernel D3 L2CAP worker.
 */
#ifndef SONATINA_AVDTP_SESSION_H
#define SONATINA_AVDTP_SESSION_H

#include "avdtp.h"

#define SONT_AVDTP_MAX_SEPS 16
#define SONT_AVDTP_MAX_PDU  512

typedef enum _SONT_AVDTP_SESSION_STATE {
    SONT_AVDTP_STATE_IDLE = 0,
    SONT_AVDTP_STATE_WAIT_DISCOVER,
    SONT_AVDTP_STATE_WAIT_GET_CAPS,
    SONT_AVDTP_STATE_WAIT_SET_CONFIG,
    SONT_AVDTP_STATE_WAIT_OPEN,
    SONT_AVDTP_STATE_WAIT_START,
    SONT_AVDTP_STATE_STREAMING,
    SONT_AVDTP_STATE_WAIT_SUSPEND,
    SONT_AVDTP_STATE_OPEN_PAUSED,
    SONT_AVDTP_STATE_WAIT_CLOSE,
    SONT_AVDTP_STATE_WAIT_ABORT,
    SONT_AVDTP_STATE_CLOSED,
    SONT_AVDTP_STATE_FAILED
} SONT_AVDTP_SESSION_STATE;

typedef enum _SONT_AVDTP_STEP_RESULT {
    SONT_AVDTP_STEP_ERROR = -1,
    SONT_AVDTP_STEP_SEND = 0,
    SONT_AVDTP_STEP_DONE = 1,
    SONT_AVDTP_STEP_IGNORE = 2
} SONT_AVDTP_STEP_RESULT;

typedef struct _SONT_AVDTP_SESSION {
    SONT_AVDTP_SESSION_STATE state;
    uint8_t local_seid;
    uint8_t preferred_codec;
    SONT_SBC_PARAMS preferred_sbc;
    SONT_LDAC_PARAMS preferred_ldac;

    uint8_t tx_counter;
    uint8_t pending_tx;
    uint8_t pending_signal;

    SONT_SEP_INFO seps[SONT_AVDTP_MAX_SEPS];
    size_t sep_count;
    size_t sep_cursor;

    uint8_t selected_acp_seid;
    SONT_CAPABILITIES negotiated;
    uint8_t last_error;
} SONT_AVDTP_SESSION;

void SontAvdtpSessionInit(SONT_AVDTP_SESSION *session,
                          uint8_t local_seid,
                          uint8_t preferred_codec,
                          const SONT_SBC_PARAMS *preferred_sbc);

void SontAvdtpSessionSetLdacPreference(SONT_AVDTP_SESSION *session,
                                       const SONT_LDAC_PARAMS *preferred_ldac);

SONT_AVDTP_STEP_RESULT SontAvdtpSessionStartHandshake(SONT_AVDTP_SESSION *session,
                                                     uint8_t *tx_out,
                                                     size_t tx_cap,
                                                     size_t *tx_len);

SONT_AVDTP_STEP_RESULT SontAvdtpSessionOnRx(SONT_AVDTP_SESSION *session,
                                            const uint8_t *rx,
                                            size_t rx_len,
                                            uint8_t *tx_out,
                                            size_t tx_cap,
                                            size_t *tx_len);

SONT_AVDTP_STEP_RESULT SontAvdtpSessionSuspend(SONT_AVDTP_SESSION *session,
                                               uint8_t *tx_out,
                                               size_t tx_cap,
                                               size_t *tx_len);

SONT_AVDTP_STEP_RESULT SontAvdtpSessionResume(SONT_AVDTP_SESSION *session,
                                              uint8_t *tx_out,
                                              size_t tx_cap,
                                              size_t *tx_len);

SONT_AVDTP_STEP_RESULT SontAvdtpSessionClose(SONT_AVDTP_SESSION *session,
                                             uint8_t *tx_out,
                                             size_t tx_cap,
                                             size_t *tx_len);

SONT_AVDTP_STEP_RESULT SontAvdtpSessionAbort(SONT_AVDTP_SESSION *session,
                                             uint8_t *tx_out,
                                             size_t tx_cap,
                                             size_t *tx_len);

#endif /* SONATINA_AVDTP_SESSION_H */
