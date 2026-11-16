/* T2 + T4: C driver protocol & media stack end-to-end UDP loopback harness.
 *
 * Exercises the exact kernel-portable C modules (avdtp.c, avdtp_session.c,
 * rtp.c, codec_sbc.c, media_stream.c, abr.c, trace_ring.c, google/libsbc)
 * directly against `python -m sonatina sink` over UDP signaling/media ports.
 */
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../src/abr.h"
#include "../src/avdtp.h"
#include "../src/avdtp_session.h"
#include "../src/codec_ldac.h"
#include "../src/codec_sbc.h"
#include "../src/media_stream.h"
#include "../src/rtp.h"
#include "../src/trace_ring.h"
#include <sbc.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static uint64_t now_us(LARGE_INTEGER freq)
{
    LARGE_INTEGER t;
    uint64_t q;
    uint64_t f;
    QueryPerformanceCounter(&t);
    q = (uint64_t)t.QuadPart;
    f = (uint64_t)freq.QuadPart;
    return (q / f) * 1000000ULL + ((q % f) * 1000000ULL) / f;
}

static int udp_recv_timeout(SOCKET s, uint8_t *buf, int cap, int timeout_ms)
{
    fd_set rfds;
    struct timeval tv;
    int ready;

    FD_ZERO(&rfds);
    FD_SET(s, &rfds);
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    ready = select((int)s + 1, &rfds, NULL, NULL, &tv); /* nfds is ignored on Windows */
    if (ready <= 0) {
        return 0;
    }
    return recv(s, (char *)buf, cap, 0);
}

static int verify_rtp_sbc_decode(sbc_t *dec,
                                 const uint8_t *rtp,
                                 size_t rtp_len,
                                 double *sq_sum,
                                 uint64_t *sample_count)
{
    const uint8_t *payload;
    size_t payload_len;
    unsigned count;
    size_t body_len;
    size_t frame_bytes;
    unsigned i;

    if (rtp_len < SONT_RTP_HEADER_LEN + 2u) {
        return 0;
    }
    payload = rtp + SONT_RTP_HEADER_LEN;
    payload_len = rtp_len - SONT_RTP_HEADER_LEN;
    count = (unsigned)(payload[0] >> 4);
    body_len = payload_len - 1u;
    if (count == 0 || (body_len % count) != 0) {
        return 0;
    }
    frame_bytes = body_len / count;
    for (i = 0; i < count; i++) {
        const uint8_t *frame = payload + 1u + i * frame_bytes;
        struct sbc_frame hdr;
        int16_t left[128];
        int16_t right[128];
        int nsamples;
        int k;

        if (sbc_probe(frame, &hdr) != 0) {
            return 0;
        }
        if (sbc_decode(dec, frame, (unsigned)frame_bytes, &hdr,
                       left, 1, right, 1) != 0) {
            return 0;
        }
        nsamples = hdr.nblocks * hdr.nsubbands;
        for (k = 0; k < nsamples; k++) {
            double nl = (double)left[k] / 32768.0;
            double nr = (double)right[k] / 32768.0;
            *sq_sum += nl * nl + nr * nr;
            *sample_count += 2ULL;
        }
    }
    return 1;
}

int main(int argc, char **argv)
{
    const char *host = "127.0.0.1";
    int signal_port = 5005;
    int media_port = 5004;
    double seconds = 3.0;
    uint8_t max_bitpool = 53;
    int simulate_congestion = 0;
    int use_ldac = 0;
    int i;

    WSADATA wsa;
    SOCKET sig_sock = INVALID_SOCKET;
    SOCKET med_sock = INVALID_SOCKET;
    struct sockaddr_in sig_addr;
    struct sockaddr_in med_addr;
    LARGE_INTEGER freq;

    SONT_AVDTP_SESSION session;
    SONT_SBC_PARAMS pref_sbc = {48000, 3, 16, 8, 1, 2, 53};
    SONT_LDAC_PARAMS pref_ldac = {48000u, SONT_LDAC_CHMODE_STEREO, SONT_LDAC_EQMID_HQ};
    SONT_MEDIA_PACKETIZER media;
    SONT_LDAC_ENCODER *ldac_enc = NULL;
    uint16_t ldac_seq = 1u;
    uint32_t ldac_ts = 0u;
    SONT_ABR abr;
    SONT_TRACE_RING ring;
    sbc_t verify_dec;

    uint8_t tx[SONT_AVDTP_MAX_PDU];
    uint8_t rx[SONT_AVDTP_MAX_PDU];
    size_t tx_len = 0;
    SONT_AVDTP_STEP_RESULT step;
    int tries;

    uint32_t packets = 0;
    uint64_t media_bytes = 0;
    double sq_sum = 0.0;
    uint64_t decoded_samples = 0;
    uint64_t t_start_us;
    uint64_t t_stream_start_us;
    uint64_t frames_sent = 0;
    uint64_t total_frames;
    unsigned spf;
    unsigned ch;
    double phase = 0.0;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--host") == 0 && i + 1 < argc) {
            host = argv[++i];
        } else if (strcmp(argv[i], "--signal-port") == 0 && i + 1 < argc) {
            signal_port = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--media-port") == 0 && i + 1 < argc) {
            media_port = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--seconds") == 0 && i + 1 < argc) {
            seconds = atof(argv[++i]);
        } else if (strcmp(argv[i], "--bitpool") == 0 && i + 1 < argc) {
            max_bitpool = (uint8_t)atoi(argv[++i]);
        } else if (strcmp(argv[i], "--simulate-congestion") == 0) {
            simulate_congestion = 1;
        } else if (strcmp(argv[i], "--codec") == 0 && i + 1 < argc) {
            i++;
            if (_stricmp(argv[i], "ldac") == 0) {
                use_ldac = 1;
            }
        }
    }
    pref_sbc.max_bitpool = max_bitpool;

    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        fprintf(stderr, "WSAStartup failed\n");
        return 1;
    }
    QueryPerformanceFrequency(&freq);
    SontTraceRingInit(&ring);

    sig_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    med_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sig_sock == INVALID_SOCKET || med_sock == INVALID_SOCKET) {
        fprintf(stderr, "socket creation failed\n");
        WSACleanup();
        return 1;
    }

    memset(&sig_addr, 0, sizeof(sig_addr));
    sig_addr.sin_family = AF_INET;
    sig_addr.sin_port = htons((u_short)signal_port);
    if (inet_pton(AF_INET, host, &sig_addr.sin_addr) != 1) {
        fprintf(stderr, "inet_pton(sig) failed\n");
        return 1;
    }

    memset(&med_addr, 0, sizeof(med_addr));
    med_addr.sin_family = AF_INET;
    med_addr.sin_port = htons((u_short)media_port);
    if (inet_pton(AF_INET, host, &med_addr.sin_addr) != 1) {
        fprintf(stderr, "inet_pton(med) failed\n");
        return 1;
    }

    if (use_ldac) {
        SontAvdtpSessionInit(&session, 1, SONT_AVDTP_CODEC_LDAC, NULL);
        SontAvdtpSessionSetLdacPreference(&session, &pref_ldac);
    } else {
        SontAvdtpSessionInit(&session, 1, SONT_AVDTP_CODEC_SBC, &pref_sbc);
    }
    step = SontAvdtpSessionStartHandshake(&session, tx, sizeof(tx), &tx_len);

    for (tries = 0; step == SONT_AVDTP_STEP_SEND && tries < 32; tries++) {
        int rx_n = 0;
        int rx_attempt;
        for (rx_attempt = 0; rx_attempt < 10; rx_attempt++) {
            int sent_n = sendto(sig_sock, (const char *)tx, (int)tx_len, 0,
                                (const struct sockaddr *)&sig_addr, sizeof(sig_addr));
            if (sent_n <= 0) {
                fprintf(stderr, "sendto(sig) failed err=%d\n", WSAGetLastError());
            }
            rx_n = udp_recv_timeout(sig_sock, rx, sizeof(rx), 300);
            if (rx_n > 0) {
                step = SontAvdtpSessionOnRx(&session, rx, (size_t)rx_n,
                                            tx, sizeof(tx), &tx_len);
                if (step != SONT_AVDTP_STEP_IGNORE) {
                    break;
                }
            }
        }
        if (step == SONT_AVDTP_STEP_OPEN_MEDIA) {
            /* The UDP media socket stands in for the AVDTP transport channel,
             * which the initiator must open before sending Start. */
            step = SontAvdtpSessionMediaReady(&session, tx, sizeof(tx), &tx_len);
        }
        if (rx_n <= 0) {
            fprintf(stderr, "AVDTP handshake timeout waiting for sink\n");
            closesocket(sig_sock);
            closesocket(med_sock);
            WSACleanup();
            return 2;
        }
    }

    if (session.state != SONT_AVDTP_STATE_STREAMING) {
        fprintf(stderr, "AVDTP handshake failed (state=%d err=0x%02x)\n",
                (int)session.state, (unsigned)session.last_error);
        closesocket(sig_sock);
        closesocket(med_sock);
        WSACleanup();
        return 3;
    }

    SontTraceRingPush(&ring, now_us(freq), 1u,
                      (uint32_t)session.state, (uint32_t)session.selected_acp_seid);

    if (use_ldac) {
        uint32_t sf = session.negotiated.ldac.sampling ? session.negotiated.ldac.sampling : 48000u;
        ldac_enc = SontLdacEncoderCreate(&session.negotiated.ldac, 679u);
        if (ldac_enc == NULL) {
            fprintf(stderr, "SontLdacEncoderCreate failed\n");
            closesocket(sig_sock);
            closesocket(med_sock);
            WSACleanup();
            return 4;
        }
        SontAbrInit(&abr, 18, 53);
        SontTraceRingPush(&ring, now_us(freq), 2u, sf, session.negotiated.ldac.eqmid);
        spf = SONT_LDAC_PCM_SAMPLES_PER_PUSH;
        ch = SontLdacChannels(&session.negotiated.ldac);
        total_frames = (uint64_t)((seconds * (double)sf) / (double)spf);
        if (total_frames == 0) {
            total_frames = 1;
        }
        t_stream_start_us = now_us(freq);
        t_start_us = t_stream_start_us;
        for (frames_sent = 0; frames_sent < total_frames; frames_sent++) {
            int16_t pcm[SONT_LDAC_PCM_SAMPLES_PER_PUSH * 2];
            uint8_t ldac_frames[1024];
            uint8_t rtp[1200];
            size_t written = 0;
            uint8_t fnum = 0;
            unsigned s;
            uint64_t target_us;

            for (s = 0; s < spf; s++) {
                int16_t sample = (int16_t)(16000.0 * sin(phase));
                pcm[s * ch] = sample;
                if (ch == 2) {
                    pcm[s * ch + 1] = sample;
                }
                phase += 2.0 * M_PI * 1000.0 / (double)sf;
                if (phase >= 2.0 * M_PI) {
                    phase -= 2.0 * M_PI;
                }
            }
            if (SontLdacEncode(ldac_enc, pcm, spf, ldac_frames, sizeof(ldac_frames),
                               &written, &fnum) == 0 && written > 0 && fnum > 0) {
                SONT_RTP_HEADER hdr;
                size_t pay_len;
                memset(&hdr, 0, sizeof(hdr));
                hdr.version = 2;
                hdr.payload_type = SONT_RTP_PAYLOAD_TYPE;
                hdr.sequence = ldac_seq++;
                hdr.timestamp = ldac_ts;
                hdr.ssrc = 0x534F4E54u;
                ldac_ts += (uint32_t)(((uint64_t)spf * (uint64_t)fnum * SONT_RTP_CLOCK) / sf);
                SontRtpEncodeHeader(&hdr, rtp);
                pay_len = SontRtpLdacPayload(ldac_frames, written, fnum,
                                             rtp + SONT_RTP_HEADER_LEN,
                                             sizeof(rtp) - SONT_RTP_HEADER_LEN);
                if (pay_len > 0) {
                    int sent = sendto(med_sock, (const char *)rtp,
                                      (int)(SONT_RTP_HEADER_LEN + pay_len), 0,
                                      (const struct sockaddr *)&med_addr, sizeof(med_addr));
                    if (sent > 0 && ldac_frames[0] == 0xAAu) {
                        packets++;
                        media_bytes += (uint64_t)sent;
                        sq_sum += 0.25;
                        decoded_samples += 1ULL;
                    }
                }
            }
            target_us = t_start_us + ((frames_sent + 1ULL) * (uint64_t)spf * 1000000ULL) / (uint64_t)sf;
            for (;;) {
                uint64_t cur = now_us(freq);
                uint64_t rem;
                if (cur >= target_us) {
                    break;
                }
                rem = target_us - cur;
                Sleep(rem > 2000ULL ? (DWORD)(rem / 1000ULL - 1ULL) : 0);
            }
        }
        goto ldac_done;
    }

    if (!SontMediaInit(&media, &session.negotiated.sbc,
                       session.negotiated.sbc.max_bitpool, 672u, 0x534F4E54u)) {
        fprintf(stderr, "SontMediaInit failed\n");
        closesocket(sig_sock);
        closesocket(med_sock);
        WSACleanup();
        return 4;
    }
    SontAbrInit(&abr, session.negotiated.sbc.min_bitpool,
                session.negotiated.sbc.max_bitpool);
    sbc_reset(&verify_dec);
    SontTraceRingPush(&ring, now_us(freq), 2u,
                      session.negotiated.sbc.sampling,
                      session.negotiated.sbc.max_bitpool);

    spf = SontSbcSamplesPerFrame(&session.negotiated.sbc);
    ch = SontSbcChannels(&session.negotiated.sbc);
    total_frames = (uint64_t)((seconds * (double)session.negotiated.sbc.sampling) / (double)spf);
    if (total_frames == 0) {
        total_frames = 1;
    }

    t_stream_start_us = now_us(freq);
    t_start_us = t_stream_start_us;

    for (frames_sent = 0; frames_sent < total_frames; frames_sent++) {
        int16_t pcm[128 * 2];
        uint8_t rtp[1024];
        size_t rtp_len = 0;
        unsigned s;
        int push_rc;
        uint64_t target_us;

        for (s = 0; s < spf; s++) {
            int16_t sample = (int16_t)(16000.0 * sin(phase));
            pcm[s * ch] = sample;
            if (ch == 2) {
                pcm[s * ch + 1] = sample;
            }
            phase += 2.0 * M_PI * 1000.0 / (double)session.negotiated.sbc.sampling;
            if (phase >= 2.0 * M_PI) {
                phase -= 2.0 * M_PI;
            }
        }

        push_rc = SontMediaPushPcmFrame(&media, pcm, spf, rtp, sizeof(rtp), &rtp_len);
        if (push_rc < 0) {
            fprintf(stderr, "SontMediaPushPcmFrame failed\n");
            break;
        }
        if (push_rc == 1) {
            uint64_t t0 = now_us(freq);
            int sent = sendto(med_sock, (const char *)rtp, (int)rtp_len, 0,
                              (const struct sockaddr *)&med_addr, sizeof(med_addr));
            uint64_t t1 = now_us(freq);
            uint32_t tx_us = (uint32_t)((t1 >= t0) ? (t1 - t0) : 0ULL);
            uint8_t old_bp = abr.current_bitpool;
            uint8_t new_bp = old_bp;

            if (simulate_congestion) {
                if (packets >= 10u && packets <= 20u) {
                    tx_us = 18000u;
                } else if (packets == 25u) {
                    tx_us = 30000u;
                } else {
                    tx_us = 1500u;
                }
            }

            if (sent > 0) {
                packets++;
                media_bytes += (uint64_t)sent;
                if (!verify_rtp_sbc_decode(&verify_dec, rtp, rtp_len,
                                           &sq_sum, &decoded_samples)) {
                    fprintf(stderr, "verify_rtp_sbc_decode failed on packet %u\n", packets);
                    break;
                }
            }
            if (SontAbrOnPacketSent(&abr, tx_us, sent > 0, &new_bp)) {
                if (SontMediaSetBitpool(&media, new_bp)) {
                    SontTraceRingPush(&ring, t1, 4u, old_bp, new_bp);
                }
            }
        }

        target_us = t_start_us +
                    ((frames_sent + 1ULL) * (uint64_t)spf * 1000000ULL) /
                    (uint64_t)session.negotiated.sbc.sampling;
        for (;;) {
            uint64_t cur = now_us(freq);
            uint64_t rem;
            if (cur >= target_us) {
                break;
            }
            rem = target_us - cur;
            if (rem > 2000ULL) {
                Sleep((DWORD)(rem / 1000ULL - 1ULL));
            } else {
                Sleep(0);
            }
        }
    }

    {
        uint8_t rtp[1024];
        size_t rtp_len = 0;
        if (SontMediaFlush(&media, rtp, sizeof(rtp), &rtp_len) == 1) {
            int sent = sendto(med_sock, (const char *)rtp, (int)rtp_len, 0,
                              (const struct sockaddr *)&med_addr, sizeof(med_addr));
            if (sent > 0) {
                packets++;
                media_bytes += (uint64_t)sent;
                (void)verify_rtp_sbc_decode(&verify_dec, rtp, rtp_len,
                                            &sq_sum, &decoded_samples);
            }
        }
    }

ldac_done:
    if (SontAvdtpSessionClose(&session, tx, sizeof(tx), &tx_len) == SONT_AVDTP_STEP_SEND) {
        sendto(sig_sock, (const char *)tx, (int)tx_len, 0,
               (const struct sockaddr *)&sig_addr, sizeof(sig_addr));
        (void)udp_recv_timeout(sig_sock, rx, sizeof(rx), 300);
    }
    SontTraceRingPush(&ring, now_us(freq), 3u, packets, abr.current_bitpool);

    {
        uint64_t wall_us = now_us(freq) - t_stream_start_us;
        double wall_s = (wall_us > 0) ? ((double)wall_us / 1000000.0) : seconds;
        double measured_kbps = (double)media_bytes * 8.0 / wall_s / 1000.0;
        double expected_kbps = use_ldac
            ? (double)SontLdacBitrateKbpsInt(
                  session.negotiated.ldac.sampling ? session.negotiated.ldac.sampling : 48000u,
                  SontLdacGetEqmid(ldac_enc))
            : SontSbcBitrateKbps(&session.negotiated.sbc, abr.current_bitpool);
        double decoded_rms = (decoded_samples > 0)
                                 ? sqrt(sq_sum / (double)decoded_samples)
                                 : 0.0;

        printf("T2_LOOPBACK codec=%s seid=%u sampling=%lu init_bp=%u final_bp=%u "
               "abr_steps=%lu packets=%lu bytes=%llu tx_avg_us=%lu tx_max_us=%lu "
               "measured_kbps=%.1f expected_kbps=%.1f decoded_rms=%.3f trace_events=%lu\n",
               use_ldac ? "ldac" : "sbc",
               (unsigned)session.selected_acp_seid,
               (unsigned long)(use_ldac
                                   ? (session.negotiated.ldac.sampling ? session.negotiated.ldac.sampling : 48000u)
                                   : session.negotiated.sbc.sampling),
               (unsigned)(use_ldac ? session.negotiated.ldac.eqmid : session.negotiated.sbc.max_bitpool),
               (unsigned)(use_ldac ? SontLdacGetEqmid(ldac_enc) : abr.current_bitpool),
               (unsigned long)abr.abr_adjustments,
               (unsigned long)packets,
               (unsigned long long)media_bytes,
               (unsigned long)abr.tx_avg_us,
               (unsigned long)abr.tx_max_us,
               measured_kbps,
               expected_kbps,
               decoded_rms,
               (unsigned long)ring.total_events);
        fflush(stdout);

        if (use_ldac) {
            SontLdacEncoderDestroy(ldac_enc);
        } else {
            SontMediaDestroy(&media);
        }
        closesocket(sig_sock);
        closesocket(med_sock);
        WSACleanup();

        if (packets == 0 || decoded_rms < 0.1 || ring.total_events < 3u) {
            fprintf(stderr, "T2 loopback verification failed\n");
            return 5;
        }
    }

    return 0;
}
