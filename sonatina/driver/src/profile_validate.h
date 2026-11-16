/* Portable profile validation and BD_ADDR formatting shared across kernel
 * driver, host unit tests, and native Win32 service.
 */
#ifndef SONATINA_PROFILE_VALIDATE_H
#define SONATINA_PROFILE_VALIDATE_H

#include "portable.h"

#ifdef __cplusplus
extern "C" {
#endif

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
                              uint32_t aac_bitrate);

/* Format lower 48 bits of `bth_addr` into 12 uppercase hex chars + NUL. */
void SontFormatBdAddrHex(uint64_t bth_addr, char out[16]);

/* Normalize "00:11:22:aa:bb:cc" / "00-11-22-AA-BB-CC" / "001122aabbcc" into
 * 12 uppercase hex chars + NUL. Returns 1 on success, 0 on invalid input. */
int SontParseBdAddrHex(const char *input, char out[16]);

#ifdef __cplusplus
}
#endif

#endif /* SONATINA_PROFILE_VALIDATE_H */
