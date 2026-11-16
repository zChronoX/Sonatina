/* Kernel control-interface IOCTLs and AVRCP devnode reconnect for SonatinaSvc. */
#ifndef SONATINA_SVC_DEVICE_CTRL_H
#define SONATINA_SVC_DEVICE_CTRL_H

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <winioctl.h>
#include "public.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*SONT_SVC_LOG_FN)(const char *msg);

/* Find the open SonatinaAudio control interface for `addr_hex`, push `profile`
 * via IOCTL_SONATINA_SET_PROFILE, and optionally issue IOCTL_SONATINA_RECONNECT.
 * Returns ERROR_SUCCESS on completion (check *matched_out for whether a live
 * driver interface matched `addr_hex`), or a Win32 error code. */
DWORD SontSvcPushProfileToDriver(const char *addr_hex,
                                 const SONATINA_PROFILE *profile,
                                 int reconnect,
                                 int *matched_out,
                                 SONT_SVC_LOG_FN log_fn);

/* Cycle any present AVRCP devnode (BTHENUM\{0000110e-...}) belonging to
 * `addr_hex` via CM_Disable_DevNode -> Sleep(100) -> CM_Enable_DevNode. */
DWORD SontSvcCycleAvrcpDevnode(const char *addr_hex,
                               int *cycled_out,
                               SONT_SVC_LOG_FN log_fn);

/* Query IOCTL_SONATINA_GET_ADDRESS + IOCTL_SONATINA_GET_STATE for all active
 * SonatinaAudio interfaces and write their Current telemetry to the registry. */
DWORD SontSvcPollDriverStates(HKEY hive,
                              const wchar_t *root,
                              size_t *updated_out);

#ifdef __cplusplus
}
#endif

#endif /* SONATINA_SVC_DEVICE_CTRL_H */
