#include "avdtp_session.h"

static uint8_t SontNextTransaction(SONT_AVDTP_SESSION *session)
{
    uint8_t tx = (uint8_t)(session->tx_counter & 0x0F);
    session->tx_counter = (uint8_t)((tx + 1) & 0x0F);
    return tx;
}

static SONT_AVDTP_STEP_RESULT SontFail(SONT_AVDTP_SESSION *session,
                                       uint8_t err,
                                       size_t *tx_len)
{
    session->state = SONT_AVDTP_STATE_FAILED;
    session->last_error = err;
    if (tx_len != NULL) {
        *tx_len = 0;
    }
    return SONT_AVDTP_STEP_ERROR;
}

static SONT_AVDTP_STEP_RESULT SontSendCommand(SONT_AVDTP_SESSION *session,
                                              uint8_t signal,
                                              const uint8_t *payload,
                                              size_t payload_len,
                                              SONT_AVDTP_SESSION_STATE next_state,
                                              uint8_t *tx_out,
                                              size_t tx_cap,
                                              size_t *tx_len)
{
    uint8_t tx;
    size_t written;

    if (tx_out == NULL || tx_len == NULL) {
        return SontFail(session, SONT_AVDTP_ERR_BAD_LENGTH, tx_len);
    }
    tx = SontNextTransaction(session);
    written = SontAvdtpEncode(tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                              signal, payload, payload_len, tx_out, tx_cap);
    if (written == 0) {
        return SontFail(session, SONT_AVDTP_ERR_BAD_LENGTH, tx_len);
    }
    session->pending_tx = tx;
    session->pending_signal = signal;
    session->state = next_state;
    *tx_len = written;
    return SONT_AVDTP_STEP_SEND;
}

static SONT_AVDTP_STEP_RESULT SontSendSingleSeidCommand(SONT_AVDTP_SESSION *session,
                                                        uint8_t signal,
                                                        uint8_t seid,
                                                        SONT_AVDTP_SESSION_STATE next_state,
                                                        uint8_t *tx_out,
                                                        size_t tx_cap,
                                                        size_t *tx_len)
{
    uint8_t body[1];
    if (SontAvdtpEncodeSingleSeid(seid, body, sizeof(body)) != 1) {
        return SontFail(session, SONT_AVDTP_ERR_BAD_ACP_SEID, tx_len);
    }
    return SontSendCommand(session, signal, body, sizeof(body), next_state,
                           tx_out, tx_cap, tx_len);
}

static bool SontIsCandidateAudioSink(const SONT_SEP_INFO *sep)
{
    return (sep->seid != 0 &&
            sep->media_type == SONT_AVDTP_MEDIA_AUDIO &&
            sep->tsep == SONT_AVDTP_TSEP_SNK &&
            !sep->in_use);
}

static SONT_AVDTP_STEP_RESULT SontProbeNextSep(SONT_AVDTP_SESSION *session,
                                              size_t start_index,
                                              uint8_t no_sep_error,
                                              uint8_t *tx_out,
                                              size_t tx_cap,
                                              size_t *tx_len)
{
    size_t i;

    for (i = start_index; i < session->sep_count; i++) {
        if (SontIsCandidateAudioSink(&session->seps[i])) {
            session->sep_cursor = i;
            return SontSendSingleSeidCommand(session,
                                             SONT_AVDTP_SIG_GET_CAPABILITIES,
                                             session->seps[i].seid,
                                             SONT_AVDTP_STATE_WAIT_GET_CAPS,
                                             tx_out, tx_cap, tx_len);
        }
    }
    return SontFail(session, no_sep_error, tx_len);
}

void SontAvdtpSessionInit(SONT_AVDTP_SESSION *session,
                          uint8_t local_seid,
                          uint8_t preferred_codec,
                          const SONT_SBC_PARAMS *preferred_sbc)
{
    if (session == NULL) {
        return;
    }
    SONT_MEMSET(session, 0, sizeof(*session));
    session->state = SONT_AVDTP_STATE_IDLE;
    session->local_seid = (local_seid >= 1 && local_seid <= 62) ? local_seid : 1;
    session->preferred_codec = preferred_codec;
    if (preferred_sbc != NULL) {
        session->preferred_sbc = *preferred_sbc;
    } else {
        session->preferred_sbc.sampling = 48000;
        session->preferred_sbc.channel_mode = 3;
        session->preferred_sbc.blocks = 16;
        session->preferred_sbc.subbands = 8;
        session->preferred_sbc.allocation = 1;
        session->preferred_sbc.min_bitpool = 2;
        session->preferred_sbc.max_bitpool = 53;
    }
    session->preferred_ldac.sampling = 48000u;
    session->preferred_ldac.channel_mode = SONT_LDAC_CHMODE_STEREO;
    session->preferred_ldac.eqmid = SONT_LDAC_EQMID_HQ;
}

void SontAvdtpSessionSetLdacPreference(SONT_AVDTP_SESSION *session,
                                       const SONT_LDAC_PARAMS *preferred_ldac)
{
    if (session == NULL || preferred_ldac == NULL) {
        return;
    }
    session->preferred_ldac = *preferred_ldac;
}

SONT_AVDTP_STEP_RESULT SontAvdtpSessionStartHandshake(SONT_AVDTP_SESSION *session,
                                                     uint8_t *tx_out,
                                                     size_t tx_cap,
                                                     size_t *tx_len)
{
    if (session == NULL || tx_len == NULL) {
        return SONT_AVDTP_STEP_ERROR;
    }
    session->sep_count = 0;
    session->sep_cursor = 0;
    session->selected_acp_seid = 0;
    session->last_error = 0;
    SONT_MEMSET(&session->negotiated, 0, sizeof(session->negotiated));

    return SontSendCommand(session, SONT_AVDTP_SIG_DISCOVER, NULL, 0,
                           SONT_AVDTP_STATE_WAIT_DISCOVER,
                           tx_out, tx_cap, tx_len);
}

static SONT_AVDTP_STEP_RESULT SontHandleDiscoverAccept(SONT_AVDTP_SESSION *session,
                                                       const SONT_AVDTP_MESSAGE *msg,
                                                       uint8_t *tx_out,
                                                       size_t tx_cap,
                                                       size_t *tx_len)
{
    bool saw_audio_sink = false;
    bool all_in_use = true;
    size_t i;

    if (!SontAvdtpParseSepList(msg->payload, msg->payload_len,
                               session->seps, SONT_AVDTP_MAX_SEPS,
                               &session->sep_count) ||
        session->sep_count == 0) {
        return SontFail(session, SONT_AVDTP_ERR_BAD_ACP_SEID, tx_len);
    }

    for (i = 0; i < session->sep_count; i++) {
        if (session->seps[i].media_type == SONT_AVDTP_MEDIA_AUDIO &&
            session->seps[i].tsep == SONT_AVDTP_TSEP_SNK) {
            saw_audio_sink = true;
            if (!session->seps[i].in_use) {
                all_in_use = false;
            }
        }
    }

    return SontProbeNextSep(session, 0,
                            (saw_audio_sink && all_in_use)
                                ? SONT_AVDTP_ERR_SEP_IN_USE
                                : SONT_AVDTP_ERR_BAD_ACP_SEID,
                            tx_out, tx_cap, tx_len);
}

static SONT_AVDTP_STEP_RESULT SontHandleGetCapsAccept(SONT_AVDTP_SESSION *session,
                                                      const SONT_AVDTP_MESSAGE *msg,
                                                      uint8_t *tx_out,
                                                      size_t tx_cap,
                                                      size_t *tx_len)
{
    SONT_CAPABILITIES caps;
    SONT_CAPABILITIES chosen;
    uint8_t cfg_payload[64];
    size_t cfg_len;
    bool matched = false;

    SONT_MEMSET(&chosen, 0, sizeof(chosen));
    if (SontAvdtpParseCapabilities(msg->payload, msg->payload_len, &caps) &&
        caps.codec == session->preferred_codec) {
        chosen.codec = caps.codec;
        if (caps.codec == SONT_AVDTP_CODEC_SBC) {
            if (caps.has_raw_sbc &&
                SontAvdtpSelectSbcConfig(caps.raw_sbc,
                                         &session->preferred_sbc,
                                         &chosen.sbc)) {
                matched = true;
            }
        } else if (caps.codec == SONT_AVDTP_CODEC_LDAC) {
            if (caps.has_raw_ldac &&
                SontAvdtpSelectLdacConfig(caps.raw_ldac,
                                          &session->preferred_ldac,
                                          &chosen.ldac)) {
                matched = true;
            }
        } else {
            matched = true;
        }
    }

    if (!matched) {
        return SontProbeNextSep(session, session->sep_cursor + 1,
                                SONT_AVDTP_ERR_INVALID_CAPABILITIES,
                                tx_out, tx_cap, tx_len);
    }

    session->selected_acp_seid = session->seps[session->sep_cursor].seid;
    session->negotiated = chosen;

    cfg_len = SontAvdtpEncodeSetConfiguration(session->selected_acp_seid,
                                              session->local_seid,
                                              &session->negotiated,
                                              cfg_payload, sizeof(cfg_payload));
    if (cfg_len == 0) {
        return SontFail(session, SONT_AVDTP_ERR_INVALID_CAPABILITIES, tx_len);
    }

    return SontSendCommand(session, SONT_AVDTP_SIG_SET_CONFIGURATION,
                           cfg_payload, cfg_len,
                           SONT_AVDTP_STATE_WAIT_SET_CONFIG,
                           tx_out, tx_cap, tx_len);
}

SONT_AVDTP_STEP_RESULT SontAvdtpSessionOnRx(SONT_AVDTP_SESSION *session,
                                            const uint8_t *rx,
                                            size_t rx_len,
                                            uint8_t *tx_out,
                                            size_t tx_cap,
                                            size_t *tx_len)
{
    SONT_AVDTP_MESSAGE msg;

    if (session == NULL || tx_len == NULL) {
        return SONT_AVDTP_STEP_ERROR;
    }
    *tx_len = 0;

    if (session->state == SONT_AVDTP_STATE_IDLE ||
        session->state == SONT_AVDTP_STATE_STREAMING ||
        session->state == SONT_AVDTP_STATE_OPEN_PAUSED ||
        session->state == SONT_AVDTP_STATE_CLOSED ||
        session->state == SONT_AVDTP_STATE_FAILED) {
        return SONT_AVDTP_STEP_IGNORE;
    }

    if (rx == NULL || !SontAvdtpDecode(rx, rx_len, &msg)) {
        return SontFail(session, SONT_AVDTP_ERR_BAD_HEADER_FORMAT, tx_len);
    }
    if (msg.packet_type != SONT_AVDTP_PT_SINGLE) {
        return SONT_AVDTP_STEP_IGNORE;
    }
    if (msg.message_type == SONT_AVDTP_MT_COMMAND) {
        return SONT_AVDTP_STEP_IGNORE;
    }
    if (msg.transaction != session->pending_tx ||
        (msg.message_type != SONT_AVDTP_MT_GENERAL_REJECT &&
         msg.signal != session->pending_signal)) {
        return SONT_AVDTP_STEP_IGNORE;
    }

    if (msg.message_type == SONT_AVDTP_MT_GENERAL_REJECT) {
        return SontFail(session, SONT_AVDTP_ERR_BAD_HEADER_FORMAT, tx_len);
    }
    if (msg.message_type == SONT_AVDTP_MT_REJECT) {
        uint8_t err = (msg.payload_len >= 1) ? msg.payload[0] : SONT_AVDTP_ERR_BAD_LENGTH;
        if (session->state == SONT_AVDTP_STATE_WAIT_GET_CAPS) {
            return SontProbeNextSep(session, session->sep_cursor + 1,
                                    err, tx_out, tx_cap, tx_len);
        }
        return SontFail(session, err, tx_len);
    }
    if (msg.message_type != SONT_AVDTP_MT_ACCEPT) {
        return SontFail(session, SONT_AVDTP_ERR_BAD_HEADER_FORMAT, tx_len);
    }

    switch (session->state) {
    case SONT_AVDTP_STATE_WAIT_DISCOVER:
        return SontHandleDiscoverAccept(session, &msg, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_STATE_WAIT_GET_CAPS:
        return SontHandleGetCapsAccept(session, &msg, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_STATE_WAIT_SET_CONFIG:
        return SontSendSingleSeidCommand(session, SONT_AVDTP_SIG_OPEN,
                                         session->selected_acp_seid,
                                         SONT_AVDTP_STATE_WAIT_OPEN,
                                         tx_out, tx_cap, tx_len);

    case SONT_AVDTP_STATE_WAIT_OPEN:
        return SontSendSingleSeidCommand(session, SONT_AVDTP_SIG_START,
                                         session->selected_acp_seid,
                                         SONT_AVDTP_STATE_WAIT_START,
                                         tx_out, tx_cap, tx_len);

    case SONT_AVDTP_STATE_WAIT_START:
        session->state = SONT_AVDTP_STATE_STREAMING;
        return SONT_AVDTP_STEP_DONE;

    case SONT_AVDTP_STATE_WAIT_SUSPEND:
        session->state = SONT_AVDTP_STATE_OPEN_PAUSED;
        return SONT_AVDTP_STEP_DONE;

    case SONT_AVDTP_STATE_WAIT_CLOSE:
    case SONT_AVDTP_STATE_WAIT_ABORT:
        session->state = SONT_AVDTP_STATE_CLOSED;
        return SONT_AVDTP_STEP_DONE;

    default:
        return SontFail(session, SONT_AVDTP_ERR_BAD_HEADER_FORMAT, tx_len);
    }
}

SONT_AVDTP_STEP_RESULT SontAvdtpSessionSuspend(SONT_AVDTP_SESSION *session,
                                               uint8_t *tx_out,
                                               size_t tx_cap,
                                               size_t *tx_len)
{
    if (session == NULL || tx_len == NULL ||
        session->state != SONT_AVDTP_STATE_STREAMING ||
        session->selected_acp_seid == 0) {
        return SONT_AVDTP_STEP_ERROR;
    }
    return SontSendSingleSeidCommand(session, SONT_AVDTP_SIG_SUSPEND,
                                     session->selected_acp_seid,
                                     SONT_AVDTP_STATE_WAIT_SUSPEND,
                                     tx_out, tx_cap, tx_len);
}

SONT_AVDTP_STEP_RESULT SontAvdtpSessionResume(SONT_AVDTP_SESSION *session,
                                              uint8_t *tx_out,
                                              size_t tx_cap,
                                              size_t *tx_len)
{
    if (session == NULL || tx_len == NULL ||
        session->state != SONT_AVDTP_STATE_OPEN_PAUSED ||
        session->selected_acp_seid == 0) {
        return SONT_AVDTP_STEP_ERROR;
    }
    return SontSendSingleSeidCommand(session, SONT_AVDTP_SIG_START,
                                     session->selected_acp_seid,
                                     SONT_AVDTP_STATE_WAIT_START,
                                     tx_out, tx_cap, tx_len);
}

SONT_AVDTP_STEP_RESULT SontAvdtpSessionClose(SONT_AVDTP_SESSION *session,
                                             uint8_t *tx_out,
                                             size_t tx_cap,
                                             size_t *tx_len)
{
    if (session == NULL || tx_len == NULL || session->selected_acp_seid == 0) {
        return SONT_AVDTP_STEP_ERROR;
    }
    if (session->state != SONT_AVDTP_STATE_STREAMING &&
        session->state != SONT_AVDTP_STATE_OPEN_PAUSED) {
        return SONT_AVDTP_STEP_ERROR;
    }
    return SontSendSingleSeidCommand(session, SONT_AVDTP_SIG_CLOSE,
                                     session->selected_acp_seid,
                                     SONT_AVDTP_STATE_WAIT_CLOSE,
                                     tx_out, tx_cap, tx_len);
}

SONT_AVDTP_STEP_RESULT SontAvdtpSessionAbort(SONT_AVDTP_SESSION *session,
                                             uint8_t *tx_out,
                                             size_t tx_cap,
                                             size_t *tx_len)
{
    if (session == NULL || tx_len == NULL || session->selected_acp_seid == 0) {
        return SONT_AVDTP_STEP_ERROR;
    }
    return SontSendSingleSeidCommand(session, SONT_AVDTP_SIG_ABORT,
                                     session->selected_acp_seid,
                                     SONT_AVDTP_STATE_WAIT_ABORT,
                                     tx_out, tx_cap, tx_len);
}
