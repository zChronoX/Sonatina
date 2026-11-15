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
#include "../src/codec_sbc.h"
#include "../src/rtp.h"

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
        assert(written > 0);
        SontSbcEncoderDestroy(enc);
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

    /* SBC math checks */
    PrintMath("sbc_default_bp53", &sbc_default, 53);
    PrintMath("sbc_default_bp24", &sbc_default, 24);
    PrintMath("sbc_44k_stereo_bp35", &sbc_44k_stereo, 35);
    PrintMath("sbc_16k_mono_bp63", &sbc_16k_mono, 63);
    PrintMath("sbc_32k_dual_bp48", &sbc_32k_dual, 48);

    printf("HOST_TESTS_OK\n");
    return 0;
}
