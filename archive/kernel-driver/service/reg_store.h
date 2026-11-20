/* Win32 registry profile and telemetry store for SonatinaSvc. */
#ifndef SONATINA_SVC_REG_STORE_H
#define SONATINA_SVC_REG_STORE_H

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <winioctl.h>
#include <stddef.h>
#include "public.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SONT_DEFAULT_REG_ROOT \
    L"SYSTEM\\CurrentControlSet\\Services\\SonatinaAudio\\Parameters"

typedef struct _SONT_DEVICE_ADDR {
    char hex[16];
} SONT_DEVICE_ADDR;

/* Ensure <root>\Devices and <root>\Control (with Trigger DWORD) exist. */
LONG SontSvcEnsureRegistry(HKEY hive, const wchar_t *root);

/* Open <root>\Control for RegNotifyChangeKeyValue. */
LONG SontSvcOpenControlKey(HKEY hive, const wchar_t *root, REGSAM sam, HKEY *key_out);

/* Enumerate valid 12-hex BD_ADDR subkeys under <root>\Devices. */
LONG SontSvcListDevices(HKEY hive,
                        const wchar_t *root,
                        SONT_DEVICE_ADDR *out,
                        size_t capacity,
                        size_t *count_out);

/* Read and validate a device profile from <root>\Devices\<addr_hex>.
 * Returns 1 if found and valid, 0 otherwise. */
int SontSvcReadProfile(HKEY hive,
                       const wchar_t *root,
                       const char *addr_hex,
                       SONATINA_PROFILE *out);

/* Write Current telemetry subkey under <root>\Devices\<addr_hex>\Current. */
LONG SontSvcWriteCurrent(HKEY hive,
                         const wchar_t *root,
                         const char *addr_hex,
                         const SONATINA_STATE *state);

#ifdef __cplusplus
}
#endif

#endif /* SONATINA_SVC_REG_STORE_H */
