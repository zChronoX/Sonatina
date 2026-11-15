/* Host unit test and byte-equality vector generator for Sonatina protocol C code.
 *
 * Compile with -DSONATINA_HOST (no SONATINA_KERNEL):
 *   avdtp.c, rtp.c, codec_sbc.c, host_test.c
 *
 * Prints `VECTOR <name> <hex>` and `MATH <name> <frame_bytes> <bitrate_kbps_x1000>`
 * lines so generate_vectors.py --verify can assert byte-for-byte equality with
 * the Python reference stack (sonatina/).
 */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../src/avdtp.h"
#include "../src/avdtp_session.h"
#include "../src/codec_sbc.h"
#include "../src/media_stream.h"
#include "../src/rtp.h"
#ifdef SONATINA_WITH_LIBSBC
#include <sbc.h>
#endif

/* Declared in avdtp.c */
size_t SontAvdtpEncodeSbcOctets(const SONT_SBC_PARAMS *params, uint8_t out[4]);

static void PrintVector(const char *name, const uint8_t *data, size_t len)
{
    size_t i;
    printf("VECTOR %s ", name);
    for (i = 0; i < len; i++) {
        printf("%02x", (unsigned)data[i]);
    }
    printf("\n");
}

static void PrintMath(const char *name, const SONT_SBC_PARAMS *params, uint8_t bitpool)
{
    unsigned frame_bytes = SontSbcFrameBytes(params, bitpool);
    double kbps = SontSbcBitrateKbps(params, bitpool);
    long long mkbps = (long long)(kbps * 1000.0 + 0.5);
    printf("MATH %s %u %lld\n", name, frame_bytes, mkbps);
}

static void FillPattern(uint8_t *buf, size_t len, uint8_t seed)
{
    size_t i;
    for (i = 0; i < len; i++) {
        buf[i] = (uint8_t)((seed + i * 7u) & 0xFFu);
    }
}

static void RunUnitAssertions(void)
{
    uint8_t buf[512];
    size_t len;
    SONT_AVDTP_MESSAGE msg;
    SONT_CAPABILITIES caps_in, caps_out;
    SONT_RTP_HEADER rtp_in, rtp_out;
    SONT_SBC_ENCODER *enc;
    int16_t pcm[256];
    size_t written = 0;

    /* 1. AVDTP encode/decode round-trip and negative checks */
    {
        uint8_t seid = 1;
        len = SontAvdtpEncode(7, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                              SONT_AVDTP_SIG_GET_CAPABILITIES, &seid, 1,
                              buf, sizeof(buf));
        assert(len == 3);
        assert(SontAvdtpDecode(buf, len, &msg));
        assert(msg.transaction == 7);
        assert(msg.packet_type == SONT_AVDTP_PT_SINGLE);
        assert(msg.message_type == SONT_AVDTP_MT_COMMAND);
        assert(msg.signal == SONT_AVDTP_SIG_GET_CAPABILITIES);
        assert(msg.payload_len == 1);
        assert(msg.payload[0] == 1);

        /* Reject non-zero RFA bits and short buffer */
        buf[1] |= 0x01;
        assert(!SontAvdtpDecode(buf, len, &msg));
        assert(!SontAvdtpDecode(buf, 1, &msg));
    }

    /* 2. AVDTP capability encode/parse round-trip and negative checks */
    {
        uint8_t bad_mt[] = {0x01, 0x01, 0x00, 0x07, 0x02, 0x00, 0xFF};
        uint8_t bad_media[] = {0x01, 0x00, 0x07, 0x02, 0x10, 0xFF};

        memset(&caps_in, 0, sizeof(caps_in));
        caps_in.codec = SONT_AVDTP_CODEC_SBC;
        caps_in.sbc.sampling = 44100;
        caps_in.sbc.channel_mode = 2; /* stereo */
        caps_in.sbc.blocks = 8;
        caps_in.sbc.subbands = 4;
        caps_in.sbc.allocation = 0; /* snr */
        caps_in.sbc.min_bitpool = 4;
        caps_in.sbc.max_bitpool = 35;

        len = SontAvdtpEncodeCapabilities(&caps_in, buf, sizeof(buf));
        assert(len == 10);
        assert(SontAvdtpParseCapabilities(buf, len, &caps_out));
        assert(caps_out.codec == SONT_AVDTP_CODEC_SBC);
        assert(caps_out.sbc.sampling == 44100);
        assert(caps_out.sbc.channel_mode == 2);
        assert(caps_out.sbc.blocks == 8);
        assert(caps_out.sbc.subbands == 4);
        assert(caps_out.sbc.allocation == 0);
        assert(caps_out.sbc.min_bitpool == 4);
        assert(caps_out.sbc.max_bitpool == 35);

        assert(!SontAvdtpParseCapabilities(bad_mt, sizeof(bad_mt), &caps_out));
        assert(!SontAvdtpParseCapabilities(bad_media, sizeof(bad_media), &caps_out));
        assert(!SontAvdtpParseCapabilities(buf, 3, &caps_out));
    }

    /* 3. RTP header encode/decode round-trip and negative checks */
    {
        memset(&rtp_in, 0, sizeof(rtp_in));
        rtp_in.version = 2;
        rtp_in.marker = true;
        rtp_in.payload_type = SONT_RTP_PAYLOAD_TYPE;
        rtp_in.sequence = 65535;
        rtp_in.timestamp = 0xFFFFFFFFu;
        rtp_in.ssrc = 0xDEADBEEFu;

        len = SontRtpEncodeHeader(&rtp_in, buf);
        assert(len == SONT_RTP_HEADER_LEN);
        assert(SontRtpDecodeHeader(buf, len, &rtp_out));
        assert(rtp_out.version == 2);
        assert(rtp_out.marker);
        assert(rtp_out.payload_type == SONT_RTP_PAYLOAD_TYPE);
        assert(rtp_out.sequence == 65535);
        assert(rtp_out.timestamp == 0xFFFFFFFFu);
        assert(rtp_out.ssrc == 0xDEADBEEFu);

        buf[0] = 0x40; /* version 1 */
        assert(!SontRtpDecodeHeader(buf, len, &rtp_out));
        assert(!SontRtpDecodeHeader(buf, 3, &rtp_out));

        /* SBC payload boundary checks */
        assert(SontRtpSbcPayload(buf, 10, 0, buf, sizeof(buf)) == 0);
        assert(SontRtpSbcPayload(buf, 10, 16, buf, sizeof(buf)) == 0);
    }

    /* 4. SBC math + encoder smoke check */
    {
        SONT_SBC_PARAMS def = {48000, 3, 16, 8, 1, 2, 53};
        assert(SontSbcChannels(&def) == 2);
        assert(SontSbcSamplesPerFrame(&def) == 128);
        assert(SontSbcFrameBytes(&def, 53) == 126);
        assert(SontSbcBitrateKbpsInt(&def, 53) == 378u);
        assert(fabs(SontSbcBitrateKbps(&def, 53) - 378.0) < 1e-6);

        memset(pcm, 0, sizeof(pcm));
        enc = SontSbcEncoderCreate(&def, 53);
        assert(enc != NULL);
        assert(SontSbcEncode(enc, pcm, 128, buf, sizeof(buf), &written) == 0);
        assert(written == SontSbcEncodedFrameBytes(&def, 53));
        assert(written == 119); /* A2DP spec SBC 48kHz joint stereo bitpool 53 */
        assert(buf[0] == 0x9C);
        SontSbcEncoderDestroy(enc);

#ifdef SONATINA_WITH_LIBSBC
        /* Encode a 1 kHz sine tone with libsbc and verify probe + decode round-trip. */
        {
            sbc_t dec;
            struct sbc_frame probed;
            int16_t dec_l[128], dec_r[128];
            int64_t energy = 0;
            size_t i;

            enc = SontSbcEncoderCreate(&def, 53);
            assert(enc != NULL);
            for (i = 0; i < 128; i++) {
                double s = sin(2.0 * 3.14159265358979323846 * 1000.0 * (double)i / 48000.0);
                int16_t v = (int16_t)(s * 12000.0);
                pcm[i * 2] = v;
                pcm[i * 2 + 1] = v;
            }
            /* Warm up filterbank with 3 frames so steady-state energy is strong. */
            for (i = 0; i < 3; i++) {
                assert(SontSbcEncode(enc, pcm, 128, buf, sizeof(buf), &written) == 0);
                assert(written == 119 && buf[0] == 0x9C);
            }
            assert(sbc_probe(buf, &probed) == 0);
            assert(probed.freq == SBC_FREQ_48K);
            assert(probed.mode == SBC_MODE_JOINT_STEREO);
            assert(probed.nblocks == 16 && probed.nsubbands == 8);
            assert(probed.bitpool == 53);

            sbc_reset(&dec);
            for (i = 0; i < 3; i++) {
                assert(SontSbcEncode(enc, pcm, 128, buf, sizeof(buf), &written) == 0);
                assert(sbc_decode(&dec, buf, (unsigned)written, &probed,
                                  dec_l, 1, dec_r, 1) == 0);
            }
            for (i = 0; i < 128; i++) {
                energy += (int64_t)dec_l[i] * (int64_t)dec_l[i];
            }
            assert(energy > 1000000LL);
            SontSbcEncoderDestroy(enc);
        }
#endif
    }

    /* 4b. PCM staging ring & D4 SBC-over-RTP packetizer */
    {
        uint8_t ring_backing[16];
        SONT_PCM_RING ring;
        uint8_t out_bytes[16];
        const uint8_t d1[10] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9};
        const uint8_t d2[10] = {10, 11, 12, 13, 14, 15, 16, 17, 18, 19};

        SontPcmRingInit(&ring, ring_backing, sizeof(ring_backing));
        assert(SontPcmRingAvailable(&ring) == 0);
        assert(SontPcmRingWrite(&ring, d1, 10) == 10);
        assert(SontPcmRingAvailable(&ring) == 10);
        /* Writing 10 more drops 4 oldest bytes of d1 cleanly. */
        assert(SontPcmRingWrite(&ring, d2, 10) == 10);
        assert(SontPcmRingAvailable(&ring) == 16);
        assert(ring.dropped_bytes == 4);
        assert(SontPcmRingRead(&ring, out_bytes, 16) == 16);
        assert(out_bytes[0] == 4 && out_bytes[5] == 9 && out_bytes[6] == 10 && out_bytes[15] == 19);
        assert(SontPcmRingAvailable(&ring) == 0);

        SONT_SBC_PARAMS def = {48000, 3, 16, 8, 1, 2, 53};
        SONT_MEDIA_PACKETIZER pkt;
        uint8_t rtp[1024];
        size_t rtp_len = 0;
        SONT_RTP_HEADER hdr;
        int f;

        assert(SontMediaInit(&pkt, &def, 53, 672, 0x12345678u));
        /* (672 - 12 - 1) / 119 = 5 SBC frames per L2CAP packet */
        assert(pkt.max_frames_per_packet == 5);
        memset(pcm, 0, sizeof(pcm));

        for (f = 0; f < 4; f++) {
            assert(SontMediaPushPcmFrame(&pkt, pcm, 128, rtp, sizeof(rtp), &rtp_len) == 0);
            assert(rtp_len == 0);
        }
        assert(SontMediaPushPcmFrame(&pkt, pcm, 128, rtp, sizeof(rtp), &rtp_len) == 1);
        assert(rtp_len == SONT_RTP_HEADER_LEN + 1u + 5u * 119u);
        assert(SontRtpDecodeHeader(rtp, rtp_len, &hdr));
        assert(hdr.marker);
        assert(hdr.sequence == 0);
        assert(hdr.timestamp == 0);
        assert(hdr.ssrc == 0x12345678u);
        assert(rtp[SONT_RTP_HEADER_LEN] == 0x50); /* 5 frames in high nibble */

        assert(SontMediaPushPcmFrame(&pkt, pcm, 128, rtp, sizeof(rtp), &rtp_len) == 0);
        assert(SontMediaFlush(&pkt, rtp, sizeof(rtp), &rtp_len) == 1);
        assert(rtp_len == SONT_RTP_HEADER_LEN + 1u + 119u);
        assert(SontRtpDecodeHeader(rtp, rtp_len, &hdr));
        assert(!hdr.marker);
        assert(hdr.sequence == 1);
        assert(hdr.timestamp == 640u);
        assert(rtp[SONT_RTP_HEADER_LEN] == 0x10);
        assert(SontMediaFlush(&pkt, rtp, sizeof(rtp), &rtp_len) == 0);

        SontMediaDestroy(&pkt);
    }

    /* 5. SEP list + single SEID + SBC capability selection */
    {
        SONT_SEP_INFO seps_in[3] = {
            {1, true,  SONT_AVDTP_MEDIA_AUDIO, SONT_AVDTP_TSEP_SNK},
            {3, false, SONT_AVDTP_MEDIA_AUDIO, SONT_AVDTP_TSEP_SRC},
            {5, false, SONT_AVDTP_MEDIA_AUDIO, SONT_AVDTP_TSEP_SNK}
        };
        SONT_SEP_INFO seps_out[4];
        size_t count = 0;
        uint8_t bad_sep[] = {0x00, 0x04};
        SONT_SBC_PARAMS pref = {48000, 3, 16, 8, 1, 2, 53};
        SONT_SBC_PARAMS chosen;
        uint8_t mask_all[4] = {0xFF, 0xFF, 2, 53};
        uint8_t mask_44k[4] = {0x22, 0x4A, 8, 35}; /* 44.1k stereo, 8 blocks, 4 subbands, SNR */
        uint8_t mask_bad_bp[4] = {0x11, 0x15, 60, 64};

        assert(SontAvdtpEncodeSingleSeid(0, buf, sizeof(buf)) == 0);
        assert(SontAvdtpEncodeSingleSeid(63, buf, sizeof(buf)) == 0);
        assert(SontAvdtpEncodeSingleSeid(5, buf, sizeof(buf)) == 1 && buf[0] == 5);

        len = SontAvdtpEncodeSepList(seps_in, 3, buf, sizeof(buf));
        assert(len == 6);
        assert(SontAvdtpParseSepList(buf, len, seps_out, 4, &count));
        assert(count == 3);
        assert(seps_out[0].seid == 1 && seps_out[0].in_use && seps_out[0].tsep == SONT_AVDTP_TSEP_SNK);
        assert(seps_out[1].seid == 3 && !seps_out[1].in_use && seps_out[1].tsep == SONT_AVDTP_TSEP_SRC);
        assert(seps_out[2].seid == 5 && !seps_out[2].in_use && seps_out[2].tsep == SONT_AVDTP_TSEP_SNK);
        assert(!SontAvdtpParseSepList(buf, 5, seps_out, 4, &count));
        assert(!SontAvdtpParseSepList(bad_sep, sizeof(bad_sep), seps_out, 4, &count));

        assert(SontAvdtpSelectSbcConfig(mask_all, &pref, &chosen));
        assert(chosen.sampling == 48000 && chosen.channel_mode == 3);
        assert(chosen.blocks == 16 && chosen.subbands == 8 && chosen.allocation == 1);
        assert(chosen.min_bitpool == 2 && chosen.max_bitpool == 53);

        assert(SontAvdtpSelectSbcConfig(mask_44k, &pref, &chosen));
        assert(chosen.sampling == 44100 && chosen.channel_mode == 2);
        assert(chosen.blocks == 8 && chosen.subbands == 4 && chosen.allocation == 0);
        assert(chosen.min_bitpool == 8 && chosen.max_bitpool == 35);

        assert(!SontAvdtpSelectSbcConfig(mask_bad_bp, &pref, &chosen));
    }

    /* 6. D3 AVDTP session state machine: probe fallback, streaming, suspend/resume/close, error paths */
    {
        SONT_AVDTP_SESSION sess;
        SONT_SBC_PARAMS pref = {48000, 3, 16, 8, 1, 2, 53};
        uint8_t tx[SONT_AVDTP_MAX_PDU], rx[SONT_AVDTP_MAX_PDU], raw[64];
        size_t tx_len = 0, rx_len = 0, raw_len = 0;

        /* 6a: Skip in-use SEP 1, SRC SEP 2, AAC-only SEP 3, and negotiate bitmask SBC on SEP 4 */
        SontAvdtpSessionInit(&sess, 1, SONT_AVDTP_CODEC_SBC, &pref);
        assert(SontAvdtpSessionStartHandshake(&sess, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        assert(sess.state == SONT_AVDTP_STATE_WAIT_DISCOVER);

        {
            SONT_SEP_INFO disc_seps[4] = {
                {1, true,  SONT_AVDTP_MEDIA_AUDIO, SONT_AVDTP_TSEP_SNK},
                {2, false, SONT_AVDTP_MEDIA_AUDIO, SONT_AVDTP_TSEP_SRC},
                {3, false, SONT_AVDTP_MEDIA_AUDIO, SONT_AVDTP_TSEP_SNK},
                {4, false, SONT_AVDTP_MEDIA_AUDIO, SONT_AVDTP_TSEP_SNK}
            };
            raw_len = SontAvdtpEncodeSepList(disc_seps, 4, raw, sizeof(raw));
            rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                     SONT_AVDTP_SIG_DISCOVER, raw, raw_len, rx, sizeof(rx));
            assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
            assert(sess.state == SONT_AVDTP_STATE_WAIT_GET_CAPS);
            assert(sess.pending_signal == SONT_AVDTP_SIG_GET_CAPABILITIES);
            assert(tx[2] == 3);
        }

        /* Wrong transaction response must be ignored without corrupting state */
        {
            uint8_t wrong_rx[4];
            size_t wrong_len = SontAvdtpEncode((uint8_t)((sess.pending_tx + 5) & 0x0F),
                                               SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                               SONT_AVDTP_SIG_GET_CAPABILITIES, NULL, 0,
                                               wrong_rx, sizeof(wrong_rx));
            assert(SontAvdtpSessionOnRx(&sess, wrong_rx, wrong_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_IGNORE);
            assert(sess.state == SONT_AVDTP_STATE_WAIT_GET_CAPS);
        }

        /* Reject SEP 3 with BAD_ACP_SEID -> session probes next SNK SEP 4 */
        {
            uint8_t err = SONT_AVDTP_ERR_BAD_ACP_SEID;
            rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_REJECT,
                                     SONT_AVDTP_SIG_GET_CAPABILITIES, &err, 1, rx, sizeof(rx));
            assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
            assert(sess.state == SONT_AVDTP_STATE_WAIT_GET_CAPS);
            assert(tx[2] == 4);
        }

        /* SEP 4 accepts with bitmask SBC capabilities -> SetConfiguration */
        {
            const uint8_t mask_caps[] = {0x01, 0x00, 0x07, 0x06, 0x00, 0x00, 0xFF, 0xFF, 2, 53};
            rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                     SONT_AVDTP_SIG_GET_CAPABILITIES, mask_caps, sizeof(mask_caps),
                                     rx, sizeof(rx));
            assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
            assert(sess.state == SONT_AVDTP_STATE_WAIT_SET_CONFIG);
            assert(sess.selected_acp_seid == 4);
            assert(sess.negotiated.codec == SONT_AVDTP_CODEC_SBC);
            assert(sess.negotiated.sbc.sampling == 48000);
        }

        /* Accept SetConfiguration -> Open -> Start -> Streaming */
        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_SET_CONFIGURATION, NULL, 0, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        assert(sess.state == SONT_AVDTP_STATE_WAIT_OPEN);

        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_OPEN, NULL, 0, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        assert(sess.state == SONT_AVDTP_STATE_WAIT_START);

        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_START, NULL, 0, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_DONE);
        assert(sess.state == SONT_AVDTP_STATE_STREAMING);

        /* Suspend -> OpenPaused -> Resume -> Streaming -> Close -> Closed */
        assert(SontAvdtpSessionSuspend(&sess, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        assert(sess.state == SONT_AVDTP_STATE_WAIT_SUSPEND);
        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_SUSPEND, NULL, 0, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_DONE);
        assert(sess.state == SONT_AVDTP_STATE_OPEN_PAUSED);

        assert(SontAvdtpSessionResume(&sess, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        assert(sess.state == SONT_AVDTP_STATE_WAIT_START);
        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_START, NULL, 0, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_DONE);
        assert(sess.state == SONT_AVDTP_STATE_STREAMING);

        assert(SontAvdtpSessionClose(&sess, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        assert(sess.state == SONT_AVDTP_STATE_WAIT_CLOSE);
        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_CLOSE, NULL, 0, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_DONE);
        assert(sess.state == SONT_AVDTP_STATE_CLOSED);

        /* 6b: Reject on Discover -> FAILED with error code */
        SontAvdtpSessionInit(&sess, 1, SONT_AVDTP_CODEC_SBC, &pref);
        assert(SontAvdtpSessionStartHandshake(&sess, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_GENERAL_REJECT,
                                 0, NULL, 0, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_ERROR);
        assert(sess.state == SONT_AVDTP_STATE_FAILED);
        assert(sess.last_error == SONT_AVDTP_ERR_BAD_HEADER_FORMAT);
    }
}

int main(void)
{
    uint8_t buf[512];
    uint8_t frames[512];
    uint8_t payload[512];
    size_t len, plen;

    const SONT_SBC_PARAMS sbc_default = {48000, 3, 16, 8, 1, 2, 53};
    const SONT_SBC_PARAMS sbc_44k_stereo = {44100, 2, 8, 4, 0, 4, 35};
    const SONT_SBC_PARAMS sbc_16k_mono = {16000, 0, 12, 8, 1, 2, 63};
    const SONT_SBC_PARAMS sbc_32k_dual = {32000, 1, 4, 4, 0, 8, 48};

    RunUnitAssertions();

    /* 1-4: SBC capability octets */
    len = SontAvdtpEncodeSbcOctets(&sbc_default, buf);
    assert(len == 4);
    PrintVector("sbc_cap_default", buf, len);

    len = SontAvdtpEncodeSbcOctets(&sbc_44k_stereo, buf);
    assert(len == 4);
    PrintVector("sbc_cap_44k_stereo", buf, len);

    len = SontAvdtpEncodeSbcOctets(&sbc_16k_mono, buf);
    assert(len == 4);
    PrintVector("sbc_cap_16k_mono", buf, len);

    len = SontAvdtpEncodeSbcOctets(&sbc_32k_dual, buf);
    assert(len == 4);
    PrintVector("sbc_cap_32k_dual", buf, len);

    /* 5-7: AVDTP Discover */
    len = SontAvdtpEncode(0, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                          SONT_AVDTP_SIG_DISCOVER, NULL, 0, buf, sizeof(buf));
    PrintVector("avdtp_discover_cmd", buf, len);

    len = SontAvdtpEncode(15, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                          SONT_AVDTP_SIG_DISCOVER, NULL, 0, buf, sizeof(buf));
    PrintVector("avdtp_discover_cmd_tx15", buf, len);

    {
        /* Sep(seid=1, in_use=0, audio=0, snk=1) -> 0x02, 0x04
         * Sep(seid=2, in_use=0, audio=0, src=0) -> 0x04, 0x00 */
        const uint8_t seps[] = {0x02, 0x04, 0x04, 0x00};
        len = SontAvdtpEncode(0, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                              SONT_AVDTP_SIG_DISCOVER, seps, sizeof(seps),
                              buf, sizeof(buf));
        PrintVector("avdtp_discover_accept_2sep", buf, len);
    }

    /* 8-9: AVDTP GetCapabilities commands */
    {
        uint8_t seid1 = 1;
        uint8_t seid7 = 7;
        len = SontAvdtpEncode(1, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                              SONT_AVDTP_SIG_GET_CAPABILITIES, &seid1, 1,
                              buf, sizeof(buf));
        PrintVector("avdtp_get_caps_cmd_seid1", buf, len);

        len = SontAvdtpEncode(7, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                              SONT_AVDTP_SIG_GET_CAPABILITIES, &seid7, 1,
                              buf, sizeof(buf));
        PrintVector("avdtp_get_caps_cmd_seid7", buf, len);
    }

    /* 10-13: AVDTP capabilities and GetCapabilities accept */
    {
        SONT_CAPABILITIES caps;
        memset(&caps, 0, sizeof(caps));
        caps.codec = SONT_AVDTP_CODEC_SBC;
        caps.sbc = sbc_default;
        len = SontAvdtpEncodeCapabilities(&caps, buf, sizeof(buf));
        PrintVector("avdtp_caps_sbc_default", buf, len);

        len = SontAvdtpEncode(1, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                              SONT_AVDTP_SIG_GET_CAPABILITIES, buf, len,
                              payload, sizeof(payload));
        PrintVector("avdtp_get_caps_accept_sbc", payload, len);

        caps.sbc = sbc_44k_stereo;
        len = SontAvdtpEncodeCapabilities(&caps, buf, sizeof(buf));
        PrintVector("avdtp_caps_sbc_44k_stereo", buf, len);

        memset(&caps, 0, sizeof(caps));
        caps.codec = SONT_AVDTP_CODEC_RAW;
        len = SontAvdtpEncodeCapabilities(&caps, buf, sizeof(buf));
        PrintVector("avdtp_caps_pcm", buf, len);
    }

    /* 14-16: AVDTP SetConfiguration */
    {
        SONT_CAPABILITIES caps;
        memset(&caps, 0, sizeof(caps));
        caps.codec = SONT_AVDTP_CODEC_SBC;
        caps.sbc = sbc_default;

        plen = SontAvdtpEncodeSetConfiguration(1, 2, &caps, payload, sizeof(payload));
        PrintVector("avdtp_set_config_payload_default", payload, plen);

        len = SontAvdtpEncode(2, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                              SONT_AVDTP_SIG_SET_CONFIGURATION, payload, plen,
                              buf, sizeof(buf));
        PrintVector("avdtp_set_config_cmd_default", buf, len);

        caps.sbc = sbc_44k_stereo;
        plen = SontAvdtpEncodeSetConfiguration(3, 1, &caps, payload, sizeof(payload));
        len = SontAvdtpEncode(3, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                              SONT_AVDTP_SIG_SET_CONFIGURATION, payload, plen,
                              buf, sizeof(buf));
        PrintVector("avdtp_set_config_cmd_44k", buf, len);
    }

    /* 17-22: Open, Start, Suspend, Close, Abort, Reject */
    {
        uint8_t seid1 = 1;
        uint8_t err_bad_acp_seid = 0x12;

        len = SontAvdtpEncode(4, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                              SONT_AVDTP_SIG_OPEN, &seid1, 1, buf, sizeof(buf));
        PrintVector("avdtp_open_cmd", buf, len);

        len = SontAvdtpEncode(5, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                              SONT_AVDTP_SIG_START, &seid1, 1, buf, sizeof(buf));
        PrintVector("avdtp_start_cmd", buf, len);

        len = SontAvdtpEncode(6, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                              SONT_AVDTP_SIG_SUSPEND, &seid1, 1, buf, sizeof(buf));
        PrintVector("avdtp_suspend_cmd", buf, len);

        len = SontAvdtpEncode(7, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                              SONT_AVDTP_SIG_CLOSE, &seid1, 1, buf, sizeof(buf));
        PrintVector("avdtp_close_cmd", buf, len);

        len = SontAvdtpEncode(8, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_COMMAND,
                              SONT_AVDTP_SIG_ABORT, &seid1, 1, buf, sizeof(buf));
        PrintVector("avdtp_abort_cmd", buf, len);

        len = SontAvdtpEncode(9, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_REJECT,
                              SONT_AVDTP_SIG_GET_CAPABILITIES, &err_bad_acp_seid, 1,
                              buf, sizeof(buf));
        PrintVector("avdtp_reject_bad_seid", buf, len);
    }

    /* 23-24: RTP headers */
    {
        SONT_RTP_HEADER hdr;
        memset(&hdr, 0, sizeof(hdr));
        hdr.version = 2;
        hdr.marker = false;
        hdr.payload_type = SONT_RTP_PAYLOAD_TYPE;
        hdr.sequence = 1;
        hdr.timestamp = 240;
        hdr.ssrc = 0x12345678u;
        len = SontRtpEncodeHeader(&hdr, buf);
        PrintVector("rtp_hdr_basic", buf, len);

        hdr.marker = true;
        hdr.sequence = 65535;
        hdr.timestamp = 0xFFFFFFFFu;
        hdr.ssrc = 0xDEADBEEFu;
        len = SontRtpEncodeHeader(&hdr, buf);
        PrintVector("rtp_hdr_max_marker", buf, len);
    }

    /* 25-28: RTP SBC payloads + full RTP packet */
    {
        SONT_RTP_HEADER hdr;

        FillPattern(frames, 126, 0x00);
        len = SontRtpSbcPayload(frames, 126, 1, buf, sizeof(buf));
        PrintVector("rtp_sbc_payload_1frame", buf, len);

        FillPattern(frames, 3 * 68, 0xA0);
        len = SontRtpSbcPayload(frames, 3 * 68, 3, buf, sizeof(buf));
        PrintVector("rtp_sbc_payload_3frames", buf, len);

        memset(&hdr, 0, sizeof(hdr));
        hdr.version = 2;
        hdr.payload_type = SONT_RTP_PAYLOAD_TYPE;
        hdr.sequence = 1;
        hdr.timestamp = 240;
        hdr.ssrc = 0x12345678u;
        SontRtpEncodeHeader(&hdr, payload);
        memcpy(payload + SONT_RTP_HEADER_LEN, buf, len);
        PrintVector("rtp_full_packet_sbc", payload, SONT_RTP_HEADER_LEN + len);

        FillPattern(frames, 15 * 14, 0x55);
        len = SontRtpSbcPayload(frames, 15 * 14, 15, buf, sizeof(buf));
        PrintVector("rtp_sbc_payload_15frames", buf, len);
    }

    /* 29-36: D3 SEP list & session handshake wire sequence */
    {
        SONT_SEP_INFO seps_multi[3] = {
            {1, true,  SONT_AVDTP_MEDIA_AUDIO, SONT_AVDTP_TSEP_SNK},
            {3, false, SONT_AVDTP_MEDIA_AUDIO, SONT_AVDTP_TSEP_SRC},
            {5, false, SONT_AVDTP_MEDIA_AUDIO, SONT_AVDTP_TSEP_SNK}
        };
        SONT_SEP_INFO disc_seps[2] = {
            {1, false, SONT_AVDTP_MEDIA_AUDIO, SONT_AVDTP_TSEP_SRC},
            {2, false, SONT_AVDTP_MEDIA_AUDIO, SONT_AVDTP_TSEP_SNK}
        };
        SONT_AVDTP_SESSION sess;
        SONT_CAPABILITIES caps;
        uint8_t rx[SONT_AVDTP_MAX_PDU], tx[SONT_AVDTP_MAX_PDU], raw[64];
        size_t rx_len, tx_len, raw_len;

        len = SontAvdtpEncodeSepList(seps_multi, 3, buf, sizeof(buf));
        PrintVector("d3_sep_list_encoded", buf, len);

        SontAvdtpSessionInit(&sess, 1, SONT_AVDTP_CODEC_SBC, &sbc_default);
        assert(SontAvdtpSessionStartHandshake(&sess, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        PrintVector("d3_hs_01_discover_cmd", tx, tx_len);

        raw_len = SontAvdtpEncodeSepList(disc_seps, 2, raw, sizeof(raw));
        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_DISCOVER, raw, raw_len, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        PrintVector("d3_hs_02_get_caps_cmd", tx, tx_len);

        memset(&caps, 0, sizeof(caps));
        caps.codec = SONT_AVDTP_CODEC_SBC;
        caps.sbc = sbc_default;
        raw_len = SontAvdtpEncodeCapabilities(&caps, raw, sizeof(raw));
        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_GET_CAPABILITIES, raw, raw_len, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        PrintVector("d3_hs_03_set_config_cmd", tx, tx_len);

        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_SET_CONFIGURATION, NULL, 0, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        PrintVector("d3_hs_04_open_cmd", tx, tx_len);

        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_OPEN, NULL, 0, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        PrintVector("d3_hs_05_start_cmd", tx, tx_len);

        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_START, NULL, 0, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_DONE);
        assert(sess.state == SONT_AVDTP_STATE_STREAMING);

        assert(SontAvdtpSessionSuspend(&sess, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        PrintVector("d3_hs_06_suspend_cmd", tx, tx_len);
        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_SUSPEND, NULL, 0, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_DONE);
        assert(sess.state == SONT_AVDTP_STATE_OPEN_PAUSED);

        assert(SontAvdtpSessionClose(&sess, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND);
        PrintVector("d3_hs_07_close_cmd", tx, tx_len);
        rx_len = SontAvdtpEncode(sess.pending_tx, SONT_AVDTP_PT_SINGLE, SONT_AVDTP_MT_ACCEPT,
                                 SONT_AVDTP_SIG_CLOSE, NULL, 0, rx, sizeof(rx));
        assert(SontAvdtpSessionOnRx(&sess, rx, rx_len, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_DONE);
        assert(sess.state == SONT_AVDTP_STATE_CLOSED);
    }

    /* SBC math checks */
    PrintMath("sbc_default_bp53", &sbc_default, 53);
    PrintMath("sbc_default_bp24", &sbc_default, 24);
    PrintMath("sbc_44k_stereo_bp35", &sbc_44k_stereo, 35);
    PrintMath("sbc_16k_mono_bp63", &sbc_16k_mono, 63);
    PrintMath("sbc_32k_dual_bp48", &sbc_32k_dual, 48);

    printf("HOST_TESTS_OK\n");
    return 0;
}
