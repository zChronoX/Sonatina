#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <winioctl.h>
#include "device_ctrl.h"
#include "reg_store.h"
#include <setupapi.h>
#include <cfgmgr32.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

const GUID GUID_DEVINTERFACE_SONATINA_CONTROL = {
    0x6e1f2c7a, 0x9b4d, 0x4e6a,
    { 0x8c, 0x31, 0x2f, 0x5b, 0x7d, 0x9a, 0x1e, 0x42 }
};

static int sont_wcsistr_contains(const wchar_t *haystack, const wchar_t *needle)
{
    size_t nlen;
    size_t i;

    if (haystack == NULL || needle == NULL) {
        return 0;
    }
    nlen = wcslen(needle);
    if (nlen == 0) {
        return 1;
    }
    for (i = 0; haystack[i] != L'\0'; i++) {
        if (_wcsnicmp(haystack + i, needle, nlen) == 0) {
            return 1;
        }
    }
    return 0;
}

static HANDLE sont_open_interface_path(const wchar_t *path)
{
    return CreateFileW(path,
                       GENERIC_READ | GENERIC_WRITE,
                       FILE_SHARE_READ | FILE_SHARE_WRITE,
                       NULL,
                       OPEN_EXISTING,
                       FILE_ATTRIBUTE_NORMAL,
                       NULL);
}

static int sont_query_address(HANDLE dev, SONATINA_DEVICE_INFO *info)
{
    DWORD bytes = 0;
    ZeroMemory(info, sizeof(*info));
    if (!DeviceIoControl(dev,
                         IOCTL_SONATINA_GET_ADDRESS,
                         NULL, 0,
                         info, sizeof(*info),
                         &bytes, NULL)) {
        return 0;
    }
    info->AddressHex[15] = '\0';
    return bytes >= sizeof(*info);
}

typedef int (*SONT_IFACE_VISITOR)(HANDLE dev,
                                  const SONATINA_DEVICE_INFO *info,
                                  void *ctx);

static DWORD sont_visit_interfaces(SONT_IFACE_VISITOR visitor, void *ctx)
{
    HDEVINFO dev_info;
    SP_DEVICE_INTERFACE_DATA if_data;
    DWORD index = 0;

    dev_info = SetupDiGetClassDevsW(&GUID_DEVINTERFACE_SONATINA_CONTROL,
                                    NULL,
                                    NULL,
                                    DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
    if (dev_info == INVALID_HANDLE_VALUE) {
        return GetLastError();
    }

    ZeroMemory(&if_data, sizeof(if_data));
    if_data.cbSize = sizeof(if_data);

    while (SetupDiEnumDeviceInterfaces(dev_info,
                                       NULL,
                                       &GUID_DEVINTERFACE_SONATINA_CONTROL,
                                       index++,
                                       &if_data)) {
        DWORD needed = 0;
        PSP_DEVICE_INTERFACE_DETAIL_DATA_W detail = NULL;
        HANDLE dev = INVALID_HANDLE_VALUE;
        SONATINA_DEVICE_INFO info;

        SetupDiGetDeviceInterfaceDetailW(dev_info, &if_data, NULL, 0, &needed, NULL);
        if (needed < sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W)) {
            continue;
        }
        detail = (PSP_DEVICE_INTERFACE_DETAIL_DATA_W)calloc(1, needed);
        if (detail == NULL) {
            SetupDiDestroyDeviceInfoList(dev_info);
            return ERROR_OUTOFMEMORY;
        }
        detail->cbSize = sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W);

        if (SetupDiGetDeviceInterfaceDetailW(dev_info, &if_data, detail, needed, NULL, NULL)) {
            dev = sont_open_interface_path(detail->DevicePath);
            if (dev != INVALID_HANDLE_VALUE) {
                if (sont_query_address(dev, &info)) {
                    int stop = visitor(dev, &info, ctx);
                    if (stop) {
                        CloseHandle(dev);
                        free(detail);
                        break;
                    }
                }
                CloseHandle(dev);
            }
        }
        free(detail);
    }

    SetupDiDestroyDeviceInfoList(dev_info);
    return ERROR_SUCCESS;
}

typedef struct _SONT_PUSH_CTX {
    const char *addr_hex;
    const SONATINA_PROFILE *profile;
    int reconnect;
    int matched;
    DWORD last_err;
    SONT_SVC_LOG_FN log_fn;
} SONT_PUSH_CTX;

static int sont_push_visitor(HANDLE dev, const SONATINA_DEVICE_INFO *info, void *raw)
{
    SONT_PUSH_CTX *ctx = (SONT_PUSH_CTX *)raw;
    DWORD bytes = 0;

    if (_stricmp(info->AddressHex, ctx->addr_hex) != 0) {
        return 0;
    }

    ctx->matched = 1;
    if (!DeviceIoControl(dev,
                         IOCTL_SONATINA_SET_PROFILE,
                         (LPVOID)ctx->profile, sizeof(*ctx->profile),
                         NULL, 0,
                         &bytes, NULL)) {
        ctx->last_err = GetLastError();
        if (ctx->log_fn != NULL) {
            char msg[160];
            _snprintf(msg, sizeof(msg),
                      "IOCTL_SONATINA_SET_PROFILE failed for %s (err=%lu)",
                      ctx->addr_hex, (unsigned long)ctx->last_err);
            msg[sizeof(msg) - 1] = '\0';
            ctx->log_fn(msg);
        }
        return 1;
    }

    if (ctx->reconnect) {
        if (!DeviceIoControl(dev,
                             IOCTL_SONATINA_RECONNECT,
                             NULL, 0,
                             NULL, 0,
                             &bytes, NULL)) {
            ctx->last_err = GetLastError();
            if (ctx->log_fn != NULL) {
                char msg[160];
                _snprintf(msg, sizeof(msg),
                          "IOCTL_SONATINA_RECONNECT failed for %s (err=%lu)",
                          ctx->addr_hex, (unsigned long)ctx->last_err);
                msg[sizeof(msg) - 1] = '\0';
                ctx->log_fn(msg);
            }
            return 1;
        }
    }
    return 1;
}

DWORD SontSvcPushProfileToDriver(const char *addr_hex,
                                 const SONATINA_PROFILE *profile,
                                 int reconnect,
                                 int *matched_out,
                                 SONT_SVC_LOG_FN log_fn)
{
    SONT_PUSH_CTX ctx;
    DWORD rc;

    if (matched_out != NULL) {
        *matched_out = 0;
    }
    if (addr_hex == NULL || profile == NULL) {
        return ERROR_INVALID_PARAMETER;
    }

    ZeroMemory(&ctx, sizeof(ctx));
    ctx.addr_hex = addr_hex;
    ctx.profile = profile;
    ctx.reconnect = reconnect;
    ctx.log_fn = log_fn;

    rc = sont_visit_interfaces(sont_push_visitor, &ctx);
    if (matched_out != NULL) {
        *matched_out = ctx.matched;
    }
    if (rc != ERROR_SUCCESS) {
        return rc;
    }
    return ctx.last_err;
}

DWORD SontSvcCycleAvrcpDevnode(const char *addr_hex,
                               int *cycled_out,
                               SONT_SVC_LOG_FN log_fn)
{
    HDEVINFO dev_info;
    SP_DEVINFO_DATA dev_data;
    wchar_t needle_addr[24];
    DWORD index = 0;
    int cycled = 0;
    int i;

    if (cycled_out != NULL) {
        *cycled_out = 0;
    }
    if (addr_hex == NULL) {
        return ERROR_INVALID_PARAMETER;
    }

    needle_addr[0] = L'&';
    for (i = 0; i < 12 && addr_hex[i] != '\0'; i++) {
        needle_addr[1 + i] = (wchar_t)toupper((unsigned char)addr_hex[i]);
    }
    needle_addr[1 + i] = L'_';
    needle_addr[2 + i] = L'\0';

    dev_info = SetupDiGetClassDevsW(NULL, L"BTHENUM", NULL,
                                    DIGCF_PRESENT | DIGCF_ALLCLASSES);
    if (dev_info == INVALID_HANDLE_VALUE) {
        return GetLastError();
    }

    ZeroMemory(&dev_data, sizeof(dev_data));
    dev_data.cbSize = sizeof(dev_data);

    while (SetupDiEnumDeviceInfo(dev_info, index++, &dev_data)) {
        wchar_t dev_id[MAX_DEVICE_ID_LEN];
        CONFIGRET cr;

        if (!SetupDiGetDeviceInstanceIdW(dev_info, &dev_data, dev_id,
                                         sizeof(dev_id) / sizeof(dev_id[0]), NULL)) {
            continue;
        }
        if (!sont_wcsistr_contains(dev_id, L"{0000110e-0000-1000-8000-00805f9b34fb}") ||
            !sont_wcsistr_contains(dev_id, needle_addr)) {
            continue;
        }

        cr = CM_Disable_DevNode(dev_data.DevInst, 0);
        if (cr == CR_SUCCESS) {
            Sleep(100);
            cr = CM_Enable_DevNode(dev_data.DevInst, 0);
            if (cr == CR_SUCCESS) {
                cycled++;
                if (log_fn != NULL) {
                    char msg[160];
                    _snprintf(msg, sizeof(msg), "cycled AVRCP devnode for %s", addr_hex);
                    msg[sizeof(msg) - 1] = '\0';
                    log_fn(msg);
                }
            } else if (log_fn != NULL) {
                char msg[160];
                _snprintf(msg, sizeof(msg),
                          "CM_Enable_DevNode failed for %s (cr=0x%lx)",
                          addr_hex, (unsigned long)cr);
                msg[sizeof(msg) - 1] = '\0';
                log_fn(msg);
            }
        } else if (log_fn != NULL) {
            char msg[160];
            _snprintf(msg, sizeof(msg),
                      "CM_Disable_DevNode failed for %s (cr=0x%lx)",
                      addr_hex, (unsigned long)cr);
            msg[sizeof(msg) - 1] = '\0';
            log_fn(msg);
        }
    }

    SetupDiDestroyDeviceInfoList(dev_info);
    if (cycled_out != NULL) {
        *cycled_out = cycled;
    }
    return ERROR_SUCCESS;
}

typedef struct _SONT_POLL_CTX {
    HKEY hive;
    const wchar_t *root;
    size_t updated;
} SONT_POLL_CTX;

static int sont_poll_visitor(HANDLE dev, const SONATINA_DEVICE_INFO *info, void *raw)
{
    SONT_POLL_CTX *ctx = (SONT_POLL_CTX *)raw;
    SONATINA_STATE state;
    DWORD bytes = 0;

    if (strcmp(info->AddressHex, "000000000000") == 0) {
        return 0;
    }

    ZeroMemory(&state, sizeof(state));
    if (DeviceIoControl(dev,
                        IOCTL_SONATINA_GET_STATE,
                        NULL, 0,
                        &state, sizeof(state),
                        &bytes, NULL) &&
        bytes >= sizeof(state)) {
        if (SontSvcWriteCurrent(ctx->hive, ctx->root, info->AddressHex, &state) == ERROR_SUCCESS) {
            ctx->updated++;
        }
    }
    return 0;
}

DWORD SontSvcPollDriverStates(HKEY hive,
                              const wchar_t *root,
                              size_t *updated_out)
{
    SONT_POLL_CTX ctx;
    DWORD rc;

    if (updated_out != NULL) {
        *updated_out = 0;
    }
    ctx.hive = hive;
    ctx.root = root;
    ctx.updated = 0;

    rc = sont_visit_interfaces(sont_poll_visitor, &ctx);
    if (updated_out != NULL) {
        *updated_out = ctx.updated;
    }
    return rc;
}
