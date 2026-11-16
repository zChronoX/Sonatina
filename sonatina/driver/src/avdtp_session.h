/* Portable AVDTP session for the A2DP source: both roles on one signalling channel.
 *
 * Initiator (INT): Discover -> GetCapabilities (probing every sink SEP, preferred
 * codec first with SBC fallback) -> SetConfiguration -> Open -> [transport
 * channel opened by the caller] -> Start, plus Suspend / Resume / Close / Abort.
 *
 * Acceptor (ACP): answers every command the headset may send (Discover,
 * GetCapabilities, GetAllCapabilities, SetConfiguration, GetConfiguration,
 * Reconfigure, Open, Start, Suspend, Close, Abort, DelayReport, SecurityControl)
 * and General Reject for unknown signals, so a headset that drives the stream
 * itself, or sends commands while we stream, is never left without an answer.
 *
 * Everything works on memory buffers so it runs unchanged in host unit tests and
 * in the kernel signalling worker.
 */
#ifndef SONATINA_AVDTP_SESSION_H
#define SONATINA_AVDTP_SESSION_H

#include "avdtp.h"

#define SONT_AVDTP_MAX_SEPS 16
#define SONT_AVDTP_MAX_PDU  512
#define SONT_AVDTP_NO_PENDING 0xFFu

typedef enum _SONT_AVDTP_SESSION_STATE {
    SONT_AVDTP_STATE_IDLE = 0,
    SONT_AVDTP_STATE_WAIT_DISCOVER,
    SONT_AVDTP_STATE_WAIT_GET_CAPS,
    SONT_AVDTP_STATE_WAIT_SET_CONFIG,
    SONT_AVDTP_STATE_WAIT_OPEN,
    SONT_AVDTP_STATE_WAIT_START,
    SONT_AVDTP_STATE_STREAMING,
    SONT_AVDTP_STATE_WAIT_SUSPEND,
    SONT_AVDTP_STATE_OPEN_PAUSED,     /* stream open, not streaming */
    SONT_AVDTP_STATE_WAIT_CLOSE,
    SONT_AVDTP_STATE_WAIT_ABORT,
    SONT_AVDTP_STATE_CLOSED,
    SONT_AVDTP_STATE_FAILED,
    SONT_AVDTP_STATE_CONFIGURED,      /* configured by the remote, waiting for its Open */
    SONT_AVDTP_STATE_OPEN_WAIT_MEDIA  /* Open accepted, transport channel not up yet */
} SONT_AVDTP_SESSION_STATE;

typedef enum _SONT_AVDTP_STEP_RESULT {
    SONT_AVDTP_STEP_ERROR = -1,
    SONT_AVDTP_STEP_SEND = 0,       /* tx holds our next command */
    SONT_AVDTP_STEP_DONE = 1,       /* our pending procedure finished */
    SONT_AVDTP_STEP_IGNORE = 2,     /* nothing to send */
    SONT_AVDTP_STEP_OPEN_MEDIA = 3, /* INT: open the transport channel, then call MediaReady */
    SONT_AVDTP_STEP_REPLY = 4       /* tx holds a response to a remote command */
} SONT_AVDTP_STEP_RESULT;

/* Event bits raised while handling traffic; read with SontAvdtpSessionTakeEvents. */
#define SONT_AVDTP_EV_CONFIGURED    0x0001u /* a stream configuration was agreed */
#define SONT_AVDTP_EV_REMOTE_OPENED 0x0002u /* remote sent Open: expect its transport channel */
#define SONT_AVDTP_EV_STARTED       0x0004u /* stream is streaming (either side started it) */
#define SONT_AVDTP_EV_SUSPENDED     0x0008u /* remote suspended the stream */
#define SONT_AVDTP_EV_CLOSED        0x0010u /* remote closed the stream */
#define SONT_AVDTP_EV_ABORTED       0x0020u /* remote aborted the stream */
#define SONT_AVDTP_EV_DELAY_REPORT  0x0040u /* remote_delay_100us updated */

typedef struct _SONT_AVDTP_SESSION {
    SONT_AVDTP_SESSION_STATE state;
    uint8_t local_seid;           /* SBC source SEP; LDAC SEP is local_seid + 1 */
    uint8_t preferred_codec;
    SONT_SBC_PARAMS preferred_sbc;
    SONT_LDAC_PARAMS preferred_ldac;
    uint8_t local_sbc_max_bitpool;
    uint8_t local_ldac_mask;      /* LDAC sampling bits we can encode, 0 = no LDAC SEP */

    uint8_t tx_counter;
    uint8_t pending_tx;           /* SONT_AVDTP_NO_PENDING when no command is outstanding */
    uint8_t pending_signal;

    SONT_SEP_INFO seps[SONT_AVDTP_MAX_SEPS];
    size_t sep_count;
    size_t sep_cursor;
    bool has_fallback;            /* an SBC configuration kept while probing for the preferred codec */
    size_t fallback_index;
    SONT_CAPABILITIES fallback_cfg;

    uint8_t selected_acp_seid;    /* remote SEP of the stream */
    uint8_t configured_local_seid;/* local SEP of the stream, 0 when none */
    bool remote_initiated;        /* the remote configured the stream */
    bool media_open;              /* transport channel is up */
    SONT_CAPABILITIES negotiated;
    uint16_t remote_delay_100us;
    uint32_t events;
    uint8_t last_error;
} SONT_AVDTP_SESSION, *PSONT_AVDTP_SESSION;

void SontAvdtpSessionInit(SONT_AVDTP_SESSION *session,
                          uint8_t local_seid,
                          uint8_t preferred_codec,
                          const SONT_SBC_PARAMS *preferred_sbc);

void SontAvdtpSessionSetLdacPreference(SONT_AVDTP_SESSION *session,
                                       const SONT_LDAC_PARAMS *preferred_ldac);

/* What the local source SEPs advertise when the headset asks (ACP role). */
void SontAvdtpSessionSetLocalCaps(SONT_AVDTP_SESSION *session,
                                  uint8_t sbc_max_bitpool,
                                  uint8_t ldac_sampling_mask);

SONT_AVDTP_STEP_RESULT SontAvdtpSessionStartHandshake(SONT_AVDTP_SESSION *session,
                                                     uint8_t *tx_out,
                                                     size_t tx_cap,
                                                     size_t *tx_len);

/* Feed one received signalling PDU (command or response). */
SONT_AVDTP_STEP_RESULT SontAvdtpSessionOnRx(SONT_AVDTP_SESSION *session,
                                            const uint8_t *rx,
                                            size_t rx_len,
                                            uint8_t *tx_out,
                                            size_t tx_cap,
                                            size_t *tx_len);

/* The transport channel is up (opened by us after OPEN_MEDIA, or accepted from
 * the remote after its Open). Returns SEND with a Start command when the stream
 * is ready to start, IGNORE otherwise. */
SONT_AVDTP_STEP_RESULT SontAvdtpSessionMediaReady(SONT_AVDTP_SESSION *session,
                                                  uint8_t *tx_out,
                                                  size_t tx_cap,
                                                  size_t *tx_len);

/* Our pending command got no answer in time. */
void SontAvdtpSessionOnTimeout(SONT_AVDTP_SESSION *session);

bool SontAvdtpSessionIsWaiting(const SONT_AVDTP_SESSION *session);
bool SontAvdtpSessionHasStream(const SONT_AVDTP_SESSION *session);
uint32_t SontAvdtpSessionTakeEvents(SONT_AVDTP_SESSION *session);

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
