#include "avdtp_session.h"

/* ------------------------------------------------------------------ helpers */

static uint8_t SontNextTransaction(SONT_AVDTP_SESSION *session)
{
    uint8_t tx = (uint8_t)(session->tx_counter & 0x0F);
    session->tx_counter = (uint8_t)((tx + 1) & 0x0F);
    return tx;
}

static void SontClearPending(SONT_AVDTP_SESSION *session)
{
    session->pending_tx = SONT_AVDTP_NO_PENDING;
    session->pending_signal = 0;
}

static void SontResetStream(SONT_AVDTP_SESSION *session)
{
    session->configured_local_seid = 0;
    session->selected_acp_seid = 0;
    session->remote_initiated = false;
    session->media_open = false;
    SONT_MEMSET(&session->negotiated, 0, sizeof(session->negotiated));
}

static SONT_AVDTP_STEP_RESULT SontFail(SONT_AVDTP_SESSION *session,
                                       uint8_t err,
                                       size_t *tx_len)
{
    session->state = SONT_AVDTP_STATE_FAILED;
    session->last_error = err;
    SontClearPending(session);
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

static SONT_AVDTP_STEP_RESULT SontReply(const SONT_AVDTP_MESSAGE *msg,
                                        uint8_t message_type,
                                        const uint8_t *payload,
                                        size_t payload_len,
                                        uint8_t *tx_out,
                                        size_t tx_cap,
                                        size_t *tx_len)
{
    size_t written = 0;

    if (tx_out != NULL) {
        written = SontAvdtpEncode(msg->transaction, SONT_AVDTP_PT_SINGLE, message_type,
                                  msg->signal, payload, payload_len, tx_out, tx_cap);
    }
    *tx_len = written;
    return (written != 0) ? SONT_AVDTP_STEP_REPLY : SONT_AVDTP_STEP_IGNORE;
}

static SONT_AVDTP_STEP_RESULT SontReplyReject1(const SONT_AVDTP_MESSAGE *msg, uint8_t err,
                                               uint8_t *tx_out, size_t tx_cap, size_t *tx_len)
{
    return SontReply(msg, SONT_AVDTP_MT_REJECT, &err, 1, tx_out, tx_cap, tx_len);
}

static SONT_AVDTP_STEP_RESULT SontReplyReject2(const SONT_AVDTP_MESSAGE *msg,
                                               uint8_t first, uint8_t err,
                                               uint8_t *tx_out, size_t tx_cap, size_t *tx_len)
{
    uint8_t body[2];
    body[0] = first;
    body[1] = err;
    return SontReply(msg, SONT_AVDTP_MT_REJECT, body, 2, tx_out, tx_cap, tx_len);
}

static uint8_t SontLocalLdacSeid(const SONT_AVDTP_SESSION *session)
{
    return (uint8_t)(session->local_seid + 1u);
}

static bool SontIsLocalSeid(const SONT_AVDTP_SESSION *session, uint8_t seid)
{
    return seid == session->local_seid ||
           (session->local_ldac_mask != 0 && seid == SontLocalLdacSeid(session));
}

static bool SontStateHasStream(SONT_AVDTP_SESSION_STATE state)
{
    switch (state) {
    case SONT_AVDTP_STATE_WAIT_SET_CONFIG:
    case SONT_AVDTP_STATE_WAIT_OPEN:
    case SONT_AVDTP_STATE_WAIT_START:
    case SONT_AVDTP_STATE_STREAMING:
    case SONT_AVDTP_STATE_WAIT_SUSPEND:
    case SONT_AVDTP_STATE_OPEN_PAUSED:
    case SONT_AVDTP_STATE_WAIT_CLOSE:
    case SONT_AVDTP_STATE_CONFIGURED:
    case SONT_AVDTP_STATE_OPEN_WAIT_MEDIA:
        return true;
    default:
        return false;
    }
}

/* ------------------------------------------------------------------ init */

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
    session->local_seid = (local_seid >= 1 && local_seid <= 61) ? local_seid : 1;
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
    session->local_sbc_max_bitpool =
        (session->preferred_sbc.max_bitpool >= 2) ? session->preferred_sbc.max_bitpool : 53;
    session->local_ldac_mask = (preferred_codec == SONT_AVDTP_CODEC_LDAC) ? 0x3Cu : 0u;
    SontClearPending(session);
}

void SontAvdtpSessionSetLdacPreference(SONT_AVDTP_SESSION *session,
                                       const SONT_LDAC_PARAMS *preferred_ldac)
{
    if (session == NULL || preferred_ldac == NULL) {
        return;
    }
    session->preferred_ldac = *preferred_ldac;
}

void SontAvdtpSessionSetLocalCaps(SONT_AVDTP_SESSION *session,
                                  uint8_t sbc_max_bitpool,
                                  uint8_t ldac_sampling_mask)
{
    if (session == NULL) {
        return;
    }
    session->local_sbc_max_bitpool = (sbc_max_bitpool >= 2) ? sbc_max_bitpool : 2;
    session->local_ldac_mask = (uint8_t)(ldac_sampling_mask & 0x3Cu);
}

bool SontAvdtpSessionIsWaiting(const SONT_AVDTP_SESSION *session)
{
    return session != NULL && session->pending_tx != SONT_AVDTP_NO_PENDING;
}

bool SontAvdtpSessionHasStream(const SONT_AVDTP_SESSION *session)
{
    return session != NULL && SontStateHasStream(session->state);
}

uint32_t SontAvdtpSessionTakeEvents(SONT_AVDTP_SESSION *session)
{
    uint32_t ev;
    if (session == NULL) {
        return 0;
    }
    ev = session->events;
    session->events = 0;
    return ev;
}

void SontAvdtpSessionOnTimeout(SONT_AVDTP_SESSION *session)
{
    if (session != NULL && session->pending_tx != SONT_AVDTP_NO_PENDING) {
        (void)SontFail(session, SONT_AVDTP_ERR_BAD_STATE, NULL);
    }
}

/* ------------------------------------------------------------------ initiator */

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
    session->has_fallback = false;
    session->last_error = 0;
    SontResetStream(session);

    return SontSendCommand(session, SONT_AVDTP_SIG_DISCOVER, NULL, 0,
                           SONT_AVDTP_STATE_WAIT_DISCOVER,
                           tx_out, tx_cap, tx_len);
}

static bool SontIsCandidateAudioSink(const SONT_SEP_INFO *sep)
{
    return (sep->seid != 0 &&
            sep->media_type == SONT_AVDTP_MEDIA_AUDIO &&
            sep->tsep == SONT_AVDTP_TSEP_SNK &&
            !sep->in_use);
}

static SONT_AVDTP_STEP_RESULT SontSendSetConfiguration(SONT_AVDTP_SESSION *session,
                                                      size_t sep_index,
                                                      const SONT_CAPABILITIES *cfg,
                                                      uint8_t *tx_out,
                                                      size_t tx_cap,
                                                      size_t *tx_len)
{
    uint8_t cfg_payload[64];
    size_t cfg_len;
    uint8_t int_seid;

    session->sep_cursor = sep_index;
    session->selected_acp_seid = session->seps[sep_index].seid;
    session->negotiated = *cfg;
    int_seid = (cfg->codec == SONT_AVDTP_CODEC_LDAC) ? SontLocalLdacSeid(session)
                                                     : session->local_seid;

    cfg_len = SontAvdtpEncodeSetConfiguration(session->selected_acp_seid, int_seid,
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
    if (session->has_fallback) {
        /* The preferred codec is not offered (e.g. LDAC switched off in the
         * headset app): use the SBC configuration found while probing. */
        SONT_CAPABILITIES cfg = session->fallback_cfg;
        session->has_fallback = false;
        return SontSendSetConfiguration(session, session->fallback_index, &cfg,
                                        tx_out, tx_cap, tx_len);
    }
    return SontFail(session, no_sep_error, tx_len);
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

    SONT_MEMSET(&chosen, 0, sizeof(chosen));
    if (SontAvdtpParseCapabilities(msg->payload, msg->payload_len, &caps)) {
        chosen.codec = caps.codec;
        if (caps.codec == session->preferred_codec) {
            bool matched = false;
            if (caps.codec == SONT_AVDTP_CODEC_SBC) {
                matched = caps.has_raw_sbc &&
                          SontAvdtpSelectSbcConfig(caps.raw_sbc, &session->preferred_sbc,
                                                   &chosen.sbc);
            } else if (caps.codec == SONT_AVDTP_CODEC_LDAC) {
                matched = caps.has_raw_ldac &&
                          SontAvdtpSelectLdacConfig(caps.raw_ldac, &session->preferred_ldac,
                                                    &chosen.ldac);
            } else {
                matched = true;
            }
            if (matched) {
                return SontSendSetConfiguration(session, session->sep_cursor, &chosen,
                                                tx_out, tx_cap, tx_len);
            }
        } else if (caps.codec == SONT_AVDTP_CODEC_SBC && !session->has_fallback &&
                   caps.has_raw_sbc &&
                   SontAvdtpSelectSbcConfig(caps.raw_sbc, &session->preferred_sbc,
                                            &chosen.sbc)) {
            session->has_fallback = true;
            session->fallback_index = session->sep_cursor;
            session->fallback_cfg = chosen;
        }
    }

    return SontProbeNextSep(session, session->sep_cursor + 1,
                            SONT_AVDTP_ERR_INVALID_CAPABILITIES,
                            tx_out, tx_cap, tx_len);
}

static SONT_AVDTP_STEP_RESULT SontStartIfReady(SONT_AVDTP_SESSION *session,
                                               uint8_t *tx_out,
                                               size_t tx_cap,
                                               size_t *tx_len)
{
    if (session->state != SONT_AVDTP_STATE_OPEN_PAUSED || !session->media_open ||
        session->pending_tx != SONT_AVDTP_NO_PENDING) {
        *tx_len = 0;
        return SONT_AVDTP_STEP_IGNORE;
    }
    return SontSendSingleSeidCommand(session, SONT_AVDTP_SIG_START,
                                     session->selected_acp_seid,
                                     SONT_AVDTP_STATE_WAIT_START,
                                     tx_out, tx_cap, tx_len);
}

SONT_AVDTP_STEP_RESULT SontAvdtpSessionMediaReady(SONT_AVDTP_SESSION *session,
                                                  uint8_t *tx_out,
                                                  size_t tx_cap,
                                                  size_t *tx_len)
{
    if (session == NULL || tx_len == NULL) {
        return SONT_AVDTP_STEP_ERROR;
    }
    *tx_len = 0;
    if (session->state != SONT_AVDTP_STATE_OPEN_WAIT_MEDIA &&
        session->state != SONT_AVDTP_STATE_OPEN_PAUSED) {
        return SONT_AVDTP_STEP_IGNORE;
    }
    session->media_open = true;
    session->state = SONT_AVDTP_STATE_OPEN_PAUSED;
    return SontStartIfReady(session, tx_out, tx_cap, tx_len);
}

static uint8_t SontRejectError(const SONT_AVDTP_MESSAGE *msg)
{
    /* Reject payloads: [error] for most signals, [first SEID or category][error]
     * for Start, Suspend, SetConfiguration and Reconfigure. */
    switch (msg->signal) {
    case SONT_AVDTP_SIG_START:
    case SONT_AVDTP_SIG_SUSPEND:
    case SONT_AVDTP_SIG_SET_CONFIGURATION:
    case SONT_AVDTP_SIG_RECONFIGURE:
        return (msg->payload_len >= 2) ? msg->payload[1] : SONT_AVDTP_ERR_BAD_LENGTH;
    default:
        return (msg->payload_len >= 1) ? msg->payload[0] : SONT_AVDTP_ERR_BAD_LENGTH;
    }
}

static SONT_AVDTP_STEP_RESULT SontHandleResponse(SONT_AVDTP_SESSION *session,
                                                 const SONT_AVDTP_MESSAGE *msg,
                                                 uint8_t *tx_out,
                                                 size_t tx_cap,
                                                 size_t *tx_len)
{
    if (session->pending_tx == SONT_AVDTP_NO_PENDING ||
        msg->transaction != session->pending_tx ||
        (msg->message_type != SONT_AVDTP_MT_GENERAL_REJECT &&
         msg->signal != session->pending_signal)) {
        return SONT_AVDTP_STEP_IGNORE;
    }
    SontClearPending(session);

    /* The headset may have started or suspended the stream itself while our
     * own command was in flight: the late answer changes nothing. */
    if (session->state == SONT_AVDTP_STATE_STREAMING ||
        (session->state == SONT_AVDTP_STATE_OPEN_PAUSED &&
         msg->signal == SONT_AVDTP_SIG_SUSPEND)) {
        return SONT_AVDTP_STEP_IGNORE;
    }

    if (msg->message_type == SONT_AVDTP_MT_GENERAL_REJECT) {
        return SontFail(session, SONT_AVDTP_ERR_NOT_SUPPORTED_COMMAND, tx_len);
    }
    if (msg->message_type == SONT_AVDTP_MT_REJECT) {
        uint8_t err = SontRejectError(msg);
        if (session->state == SONT_AVDTP_STATE_WAIT_GET_CAPS) {
            return SontProbeNextSep(session, session->sep_cursor + 1,
                                    err, tx_out, tx_cap, tx_len);
        }
        if (session->state == SONT_AVDTP_STATE_WAIT_START) {
            /* Stream stays open; the caller may retry Start later. */
            session->state = SONT_AVDTP_STATE_OPEN_PAUSED;
            session->last_error = err;
            return SONT_AVDTP_STEP_ERROR;
        }
        return SontFail(session, err, tx_len);
    }
    if (msg->message_type != SONT_AVDTP_MT_ACCEPT) {
        return SontFail(session, SONT_AVDTP_ERR_BAD_HEADER_FORMAT, tx_len);
    }

    switch (session->state) {
    case SONT_AVDTP_STATE_WAIT_DISCOVER:
        return SontHandleDiscoverAccept(session, msg, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_STATE_WAIT_GET_CAPS:
        return SontHandleGetCapsAccept(session, msg, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_STATE_WAIT_SET_CONFIG:
        session->configured_local_seid =
            (session->negotiated.codec == SONT_AVDTP_CODEC_LDAC) ? SontLocalLdacSeid(session)
                                                                : session->local_seid;
        session->events |= SONT_AVDTP_EV_CONFIGURED;
        return SontSendSingleSeidCommand(session, SONT_AVDTP_SIG_OPEN,
                                         session->selected_acp_seid,
                                         SONT_AVDTP_STATE_WAIT_OPEN,
                                         tx_out, tx_cap, tx_len);

    case SONT_AVDTP_STATE_WAIT_OPEN:
        /* AVDTP: the initiator opens the transport channel before Start. */
        session->state = SONT_AVDTP_STATE_OPEN_WAIT_MEDIA;
        return SONT_AVDTP_STEP_OPEN_MEDIA;

    case SONT_AVDTP_STATE_WAIT_START:
        session->state = SONT_AVDTP_STATE_STREAMING;
        session->events |= SONT_AVDTP_EV_STARTED;
        return SONT_AVDTP_STEP_DONE;

    case SONT_AVDTP_STATE_WAIT_SUSPEND:
        session->state = SONT_AVDTP_STATE_OPEN_PAUSED;
        return SONT_AVDTP_STEP_DONE;

    case SONT_AVDTP_STATE_WAIT_CLOSE:
    case SONT_AVDTP_STATE_WAIT_ABORT:
        SontResetStream(session);
        session->state = SONT_AVDTP_STATE_CLOSED;
        return SONT_AVDTP_STEP_DONE;

    default:
        return SONT_AVDTP_STEP_IGNORE;
    }
}

/* ------------------------------------------------------------------ acceptor */

static SONT_AVDTP_STEP_RESULT SontHandleSetConfiguration(SONT_AVDTP_SESSION *session,
                                                        const SONT_AVDTP_MESSAGE *msg,
                                                        uint8_t *tx_out,
                                                        size_t tx_cap,
                                                        size_t *tx_len)
{
    SONT_CAPABILITIES cfg;
    uint8_t acp_seid;
    uint8_t int_seid;

    if (msg->payload_len < 2) {
        return SontReplyReject2(msg, 0, SONT_AVDTP_ERR_BAD_LENGTH, tx_out, tx_cap, tx_len);
    }
    acp_seid = (uint8_t)((msg->payload[0] >> 2) & 0x3F);
    int_seid = (uint8_t)((msg->payload[1] >> 2) & 0x3F);
    if (!SontIsLocalSeid(session, acp_seid) || int_seid == 0) {
        return SontReplyReject2(msg, 0, SONT_AVDTP_ERR_BAD_ACP_SEID, tx_out, tx_cap, tx_len);
    }
    /* Collision rule: once our own SetConfiguration is out (or a stream exists)
     * the remote has to wait; before that the remote's choice wins. */
    if (session->configured_local_seid != 0 || SontStateHasStream(session->state)) {
        return SontReplyReject2(msg, 0, SONT_AVDTP_ERR_SEP_IN_USE, tx_out, tx_cap, tx_len);
    }
    if (!SontAvdtpParseCapabilities(msg->payload + 2, msg->payload_len - 2, &cfg)) {
        return SontReplyReject2(msg, SONT_AVDTP_CAT_MEDIA_CODEC,
                                SONT_AVDTP_ERR_INVALID_CAPABILITIES, tx_out, tx_cap, tx_len);
    }
    if ((acp_seid == session->local_seid && cfg.codec != SONT_AVDTP_CODEC_SBC) ||
        (acp_seid != session->local_seid && cfg.codec != SONT_AVDTP_CODEC_LDAC) ||
        !SontAvdtpSourceAcceptsConfig(&cfg, session->local_sbc_max_bitpool,
                                      session->local_ldac_mask)) {
        return SontReplyReject2(msg, SONT_AVDTP_CAT_MEDIA_CODEC,
                                SONT_AVDTP_ERR_UNSUPPORTED_CONFIGURATION,
                                tx_out, tx_cap, tx_len);
    }
    if (cfg.codec == SONT_AVDTP_CODEC_LDAC) {
        cfg.ldac.eqmid = session->preferred_ldac.eqmid;
    }

    SontClearPending(session);  /* any handshake of ours is abandoned */
    session->negotiated = cfg;
    session->configured_local_seid = acp_seid;
    session->selected_acp_seid = int_seid;
    session->remote_initiated = true;
    session->media_open = false;
    session->state = SONT_AVDTP_STATE_CONFIGURED;
    session->events |= SONT_AVDTP_EV_CONFIGURED;
    return SontReply(msg, SONT_AVDTP_MT_ACCEPT, NULL, 0, tx_out, tx_cap, tx_len);
}

static SONT_AVDTP_STEP_RESULT SontHandleCommand(SONT_AVDTP_SESSION *session,
                                                const SONT_AVDTP_MESSAGE *msg,
                                                uint8_t *tx_out,
                                                size_t tx_cap,
                                                size_t *tx_len)
{
    uint8_t body[64];
    size_t n = 0;
    uint8_t first = (msg->payload_len >= 1) ? msg->payload[0] : 0;
    uint8_t seid = (uint8_t)((first >> 2) & 0x3F);
    bool ours = (session->configured_local_seid != 0 &&
                 seid == session->configured_local_seid);

    switch (msg->signal) {
    case SONT_AVDTP_SIG_DISCOVER: {
        SONT_SEP_INFO seps[2];
        size_t count = 0;
        seps[count].seid = session->local_seid;
        seps[count].in_use = (session->configured_local_seid == session->local_seid);
        seps[count].media_type = SONT_AVDTP_MEDIA_AUDIO;
        seps[count].tsep = SONT_AVDTP_TSEP_SRC;
        count++;
        if (session->local_ldac_mask != 0) {
            seps[count].seid = SontLocalLdacSeid(session);
            seps[count].in_use = (session->configured_local_seid == SontLocalLdacSeid(session));
            seps[count].media_type = SONT_AVDTP_MEDIA_AUDIO;
            seps[count].tsep = SONT_AVDTP_TSEP_SRC;
            count++;
        }
        n = SontAvdtpEncodeSepList(seps, count, body, sizeof(body));
        return SontReply(msg, SONT_AVDTP_MT_ACCEPT, body, n, tx_out, tx_cap, tx_len);
    }

    case SONT_AVDTP_SIG_GET_CAPABILITIES:
    case SONT_AVDTP_SIG_GET_ALL_CAPABILITIES:
        if (msg->payload_len < 1) {
            return SontReplyReject1(msg, SONT_AVDTP_ERR_BAD_LENGTH, tx_out, tx_cap, tx_len);
        }
        if (seid == session->local_seid) {
            n = SontAvdtpEncodeSourceSbcCapabilities(session->local_sbc_max_bitpool,
                                                     body, sizeof(body));
        } else if (SontIsLocalSeid(session, seid)) {
            n = SontAvdtpEncodeSourceLdacCapabilities(session->local_ldac_mask,
                                                      body, sizeof(body));
        }
        if (n == 0) {
            return SontReplyReject1(msg, SONT_AVDTP_ERR_BAD_ACP_SEID, tx_out, tx_cap, tx_len);
        }
        return SontReply(msg, SONT_AVDTP_MT_ACCEPT, body, n, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_SIG_SET_CONFIGURATION:
        return SontHandleSetConfiguration(session, msg, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_SIG_GET_CONFIGURATION:
        if (!ours) {
            return SontReplyReject1(msg, SONT_AVDTP_ERR_BAD_ACP_SEID, tx_out, tx_cap, tx_len);
        }
        n = SontAvdtpEncodeCapabilities(&session->negotiated, body, sizeof(body));
        return SontReply(msg, SONT_AVDTP_MT_ACCEPT, body, n, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_SIG_RECONFIGURE:
        return SontReplyReject2(msg, SONT_AVDTP_CAT_MEDIA_CODEC,
                                SONT_AVDTP_ERR_UNSUPPORTED_CONFIGURATION,
                                tx_out, tx_cap, tx_len);

    case SONT_AVDTP_SIG_OPEN:
        if (!ours) {
            return SontReplyReject1(msg, SONT_AVDTP_ERR_BAD_ACP_SEID, tx_out, tx_cap, tx_len);
        }
        if (session->state != SONT_AVDTP_STATE_CONFIGURED) {
            return SontReplyReject1(msg, SONT_AVDTP_ERR_BAD_STATE, tx_out, tx_cap, tx_len);
        }
        session->state = SONT_AVDTP_STATE_OPEN_WAIT_MEDIA;
        session->events |= SONT_AVDTP_EV_REMOTE_OPENED;
        return SontReply(msg, SONT_AVDTP_MT_ACCEPT, NULL, 0, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_SIG_START:
        if (!ours) {
            return SontReplyReject2(msg, first, SONT_AVDTP_ERR_BAD_ACP_SEID, tx_out, tx_cap, tx_len);
        }
        if (session->state == SONT_AVDTP_STATE_STREAMING) {
            return SontReply(msg, SONT_AVDTP_MT_ACCEPT, NULL, 0, tx_out, tx_cap, tx_len);
        }
        if (!session->media_open ||
            (session->state != SONT_AVDTP_STATE_OPEN_PAUSED &&
             session->state != SONT_AVDTP_STATE_WAIT_START)) {
            return SontReplyReject2(msg, first, SONT_AVDTP_ERR_BAD_STATE, tx_out, tx_cap, tx_len);
        }
        session->state = SONT_AVDTP_STATE_STREAMING;
        session->events |= SONT_AVDTP_EV_STARTED;
        return SontReply(msg, SONT_AVDTP_MT_ACCEPT, NULL, 0, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_SIG_SUSPEND:
        if (!ours) {
            return SontReplyReject2(msg, first, SONT_AVDTP_ERR_BAD_ACP_SEID, tx_out, tx_cap, tx_len);
        }
        if (session->state == SONT_AVDTP_STATE_STREAMING ||
            session->state == SONT_AVDTP_STATE_WAIT_START ||
            session->state == SONT_AVDTP_STATE_WAIT_SUSPEND) {
            session->state = SONT_AVDTP_STATE_OPEN_PAUSED;
            session->events |= SONT_AVDTP_EV_SUSPENDED;
        } else if (session->state != SONT_AVDTP_STATE_OPEN_PAUSED) {
            return SontReplyReject2(msg, first, SONT_AVDTP_ERR_BAD_STATE, tx_out, tx_cap, tx_len);
        }
        return SontReply(msg, SONT_AVDTP_MT_ACCEPT, NULL, 0, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_SIG_CLOSE:
        if (!ours) {
            return SontReplyReject1(msg, SONT_AVDTP_ERR_BAD_ACP_SEID, tx_out, tx_cap, tx_len);
        }
        SontClearPending(session);
        SontResetStream(session);
        session->state = SONT_AVDTP_STATE_CLOSED;
        session->events |= SONT_AVDTP_EV_CLOSED;
        return SontReply(msg, SONT_AVDTP_MT_ACCEPT, NULL, 0, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_SIG_ABORT:
        /* Abort is never rejected. */
        if (ours || SontStateHasStream(session->state)) {
            SontClearPending(session);
            SontResetStream(session);
            session->state = SONT_AVDTP_STATE_CLOSED;
            session->events |= SONT_AVDTP_EV_ABORTED;
        }
        return SontReply(msg, SONT_AVDTP_MT_ACCEPT, NULL, 0, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_SIG_DELAY_REPORT:
        if (msg->payload_len < 3) {
            return SontReplyReject1(msg, SONT_AVDTP_ERR_BAD_LENGTH, tx_out, tx_cap, tx_len);
        }
        session->remote_delay_100us = (uint16_t)(((uint16_t)msg->payload[1] << 8) | msg->payload[2]);
        session->events |= SONT_AVDTP_EV_DELAY_REPORT;
        return SontReply(msg, SONT_AVDTP_MT_ACCEPT, NULL, 0, tx_out, tx_cap, tx_len);

    case SONT_AVDTP_SIG_SECURITY_CONTROL:
        return SontReplyReject1(msg, SONT_AVDTP_ERR_BAD_ACP_SEID, tx_out, tx_cap, tx_len);

    default:
        return SontReply(msg, SONT_AVDTP_MT_GENERAL_REJECT, NULL, 0, tx_out, tx_cap, tx_len);
    }
}

/* ------------------------------------------------------------------ dispatch */

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

    if (rx == NULL || !SontAvdtpDecode(rx, rx_len, &msg)) {
        return SONT_AVDTP_STEP_IGNORE;  /* malformed: nothing we can answer */
    }
    if (msg.packet_type != SONT_AVDTP_PT_SINGLE) {
        /* Fragmented signalling is not needed for A2DP (PDUs fit the MTU);
         * answer fragmented commands is impossible without reassembly. */
        return SONT_AVDTP_STEP_IGNORE;
    }
    if (msg.message_type == SONT_AVDTP_MT_COMMAND) {
        return SontHandleCommand(session, &msg, tx_out, tx_cap, tx_len);
    }
    return SontHandleResponse(session, &msg, tx_out, tx_cap, tx_len);
}

/* ------------------------------------------------------------------ local actions */

SONT_AVDTP_STEP_RESULT SontAvdtpSessionSuspend(SONT_AVDTP_SESSION *session,
                                               uint8_t *tx_out,
                                               size_t tx_cap,
                                               size_t *tx_len)
{
    if (session == NULL || tx_len == NULL ||
        session->state != SONT_AVDTP_STATE_STREAMING ||
        session->selected_acp_seid == 0 ||
        session->pending_tx != SONT_AVDTP_NO_PENDING) {
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
        session->selected_acp_seid == 0 || !session->media_open) {
        return SONT_AVDTP_STEP_ERROR;
    }
    return SontStartIfReady(session, tx_out, tx_cap, tx_len);
}

SONT_AVDTP_STEP_RESULT SontAvdtpSessionClose(SONT_AVDTP_SESSION *session,
                                             uint8_t *tx_out,
                                             size_t tx_cap,
                                             size_t *tx_len)
{
    if (session == NULL || tx_len == NULL || session->selected_acp_seid == 0) {
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
