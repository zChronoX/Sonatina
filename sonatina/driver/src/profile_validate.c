#include "profile_validate.h"

static int sont_hex_val(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return 10 + (c - 'a');
    }
    if (c >= 'A' && c <= 'F') {
        return 10 + (c - 'A');
    }
    return -1;
}

static int sont_is_sep(char c)
{
    return (c == ':' || c == '-' || c == '.' || c == ' ');
}

int SontProfileValidateFields(uint32_t codec,
                              uint32_t sbc_sampling,
                              uint32_t sbc_channel_mode,
                              uint32_t sbc_blocks,
                              uint32_t sbc_subbands,
                              uint32_t sbc_allocation,
                              uint32_t sbc_min_bitpool,
                              uint32_t sbc_max_bitpool,
                              uint32_t aac_sampling,
                              uint32_t aac_channels,
                              uint32_t aac_bitrate)
{
    if (codec > 4u) {
        return 0;
    }

    if (sbc_sampling != 16000u && sbc_sampling != 32000u &&
        sbc_sampling != 44100u && sbc_sampling != 48000u) {
        return 0;
    }
    if (sbc_channel_mode > 3u) {
        return 0;
    }
    if (sbc_blocks != 4u && sbc_blocks != 8u &&
        sbc_blocks != 12u && sbc_blocks != 16u) {
        return 0;
    }
    if (sbc_subbands != 4u && sbc_subbands != 8u) {
        return 0;
    }
    if (sbc_allocation > 1u) {
        return 0;
    }
    if (sbc_min_bitpool < 2u || sbc_min_bitpool > 250u) {
        return 0;
    }
    if (sbc_max_bitpool < sbc_min_bitpool || sbc_max_bitpool > 250u) {
        return 0;
    }

    if (codec == 2u) { /* AAC */
        if (aac_sampling != 44100u && aac_sampling != 48000u) {
            return 0;
        }
        if (aac_channels != 1u && aac_channels != 2u) {
            return 0;
        }
        if (aac_bitrate < 32u || aac_bitrate > 1024u) {
            return 0;
        }
    }

    return 1;
}

void SontFormatBdAddrHex(uint64_t bth_addr, char out[16])
{
    static const char hex[] = "0123456789ABCDEF";
    int i;

    if (out == NULL) {
        return;
    }
    for (i = 0; i < 12; i++) {
        unsigned shift = (unsigned)((11 - i) * 4);
        out[i] = hex[(unsigned)((bth_addr >> shift) & 0xFULL)];
    }
    out[12] = '\0';
}

int SontParseBdAddrHex(const char *input, char out[16])
{
    static const char hex[] = "0123456789ABCDEF";
    char buf[13];
    size_t i = 0;
    size_t n = 0;

    if (input == NULL || out == NULL) {
        return 0;
    }

    while (input[i] == ' ' || input[i] == '\t') {
        i++;
    }

    for (n = 0; n < 6; n++) {
        int hi = sont_hex_val(input[i]);
        int lo;
        if (hi < 0) {
            return 0;
        }
        lo = sont_hex_val(input[i + 1]);
        if (lo < 0) {
            return 0;
        }
        buf[n * 2] = hex[hi];
        buf[n * 2 + 1] = hex[lo];
        i += 2;
        if (n < 5 && sont_is_sep(input[i])) {
            i++;
        }
    }

    while (input[i] == ' ' || input[i] == '\t') {
        i++;
    }
    if (input[i] != '\0') {
        return 0;
    }

    buf[12] = '\0';
    SONT_MEMCPY(out, buf, 13);
    return 1;
}
