#include "reg_store.h"
#include "profile_validate.h"
#include <stdio.h>
#include <string.h>

static void sont_init_default_profile(SONATINA_PROFILE *prof)
{
    ZeroMemory(prof, sizeof(*prof));
    prof->Size = sizeof(SONATINA_PROFILE);
    prof->Codec = SonatinaCodecSbc;
    prof->SbcSampling = 48000;
    prof->SbcChannelMode = 3;
    prof->SbcBlocks = 16;
    prof->SbcSubbands = 8;
    prof->SbcAllocation = 1;
    prof->SbcMinBitpool = 2;
    prof->SbcMaxBitpool = 53;
    prof->AacSampling = 48000;
    prof->AacChannels = 2;
    prof->AacBitrate = 256;
}

static LONG sont_build_subpath(const wchar_t *root,
                               const wchar_t *suffix,
                               wchar_t *out,
                               size_t out_chars)
{
    int n = _snwprintf(out, out_chars, L"%ls\\%ls", root, suffix);
    if (n < 0 || (size_t)n >= out_chars) {
        return ERROR_BUFFER_OVERFLOW;
    }
    return ERROR_SUCCESS;
}

static void sont_read_dword(HKEY key, const wchar_t *name, DWORD *val)
{
    DWORD type = 0;
    DWORD data = 0;
    DWORD size = sizeof(data);
    if (RegQueryValueExW(key, name, NULL, &type, (LPBYTE)&data, &size) == ERROR_SUCCESS &&
        type == REG_DWORD && size == sizeof(DWORD)) {
        *val = data;
    }
}

static const wchar_t *sont_codec_name_w(ULONG codec)
{
    switch (codec) {
    case SonatinaCodecPcm:  return L"pcm";
    case SonatinaCodecSbc:  return L"sbc";
    case SonatinaCodecAac:  return L"aac";
    case SonatinaCodecAptx: return L"aptx";
    case SonatinaCodecLdac: return L"ldac";
    default:                return L"sbc";
    }
}

LONG SontSvcEnsureRegistry(HKEY hive, const wchar_t *root)
{
    wchar_t path[512];
    HKEY key = NULL;
    LONG rc;

    rc = sont_build_subpath(root, L"Devices", path, sizeof(path) / sizeof(path[0]));
    if (rc != ERROR_SUCCESS) {
        return rc;
    }
    rc = RegCreateKeyExW(hive, path, 0, NULL, REG_OPTION_NON_VOLATILE,
                         KEY_READ | KEY_WRITE, NULL, &key, NULL);
    if (rc != ERROR_SUCCESS) {
        return rc;
    }
    RegCloseKey(key);
    key = NULL;

    rc = sont_build_subpath(root, L"Control", path, sizeof(path) / sizeof(path[0]));
    if (rc != ERROR_SUCCESS) {
        return rc;
    }
    rc = RegCreateKeyExW(hive, path, 0, NULL, REG_OPTION_NON_VOLATILE,
                         KEY_READ | KEY_WRITE, NULL, &key, NULL);
    if (rc != ERROR_SUCCESS) {
        return rc;
    }
    {
        DWORD type = 0;
        DWORD val = 0;
        DWORD sz = sizeof(val);
        if (RegQueryValueExW(key, L"Trigger", NULL, &type, (LPBYTE)&val, &sz) != ERROR_SUCCESS) {
            DWORD zero = 0;
            (void)RegSetValueExW(key, L"Trigger", 0, REG_DWORD, (const BYTE *)&zero, sizeof(zero));
        }
    }
    RegCloseKey(key);
    return ERROR_SUCCESS;
}

LONG SontSvcOpenControlKey(HKEY hive, const wchar_t *root, REGSAM sam, HKEY *key_out)
{
    wchar_t path[512];
    LONG rc;

    if (key_out == NULL) {
        return ERROR_INVALID_PARAMETER;
    }
    *key_out = NULL;
    rc = sont_build_subpath(root, L"Control", path, sizeof(path) / sizeof(path[0]));
    if (rc != ERROR_SUCCESS) {
        return rc;
    }
    return RegOpenKeyExW(hive, path, 0, sam, key_out);
}

LONG SontSvcListDevices(HKEY hive,
                        const wchar_t *root,
                        SONT_DEVICE_ADDR *out,
                        size_t capacity,
                        size_t *count_out)
{
    wchar_t path[512];
    HKEY key = NULL;
    LONG rc;
    DWORD idx = 0;
    size_t count = 0;

    if (count_out != NULL) {
        *count_out = 0;
    }
    rc = sont_build_subpath(root, L"Devices", path, sizeof(path) / sizeof(path[0]));
    if (rc != ERROR_SUCCESS) {
        return rc;
    }
    rc = RegOpenKeyExW(hive, path, 0, KEY_READ, &key);
    if (rc != ERROR_SUCCESS) {
        return rc;
    }

    for (;;) {
        wchar_t name_w[64];
        DWORD name_len = (DWORD)(sizeof(name_w) / sizeof(name_w[0]));
        char name_a[64];
        char norm[16];
        size_t i;

        rc = RegEnumKeyExW(key, idx++, name_w, &name_len, NULL, NULL, NULL, NULL);
        if (rc == ERROR_NO_MORE_ITEMS) {
            rc = ERROR_SUCCESS;
            break;
        }
        if (rc != ERROR_SUCCESS) {
            break;
        }
        if (name_len >= sizeof(name_a)) {
            continue;
        }
        for (i = 0; i < name_len; i++) {
            name_a[i] = (name_w[i] <= 0x7F) ? (char)name_w[i] : '?';
        }
        name_a[name_len] = '\0';
        if (SontParseBdAddrHex(name_a, norm) && out != NULL && count < capacity) {
            memcpy(out[count].hex, norm, 16);
            count++;
        }
    }

    RegCloseKey(key);
    if (count_out != NULL) {
        *count_out = count;
    }
    return rc;
}

int SontSvcReadProfile(HKEY hive,
                       const wchar_t *root,
                       const char *addr_hex,
                       SONATINA_PROFILE *out)
{
    wchar_t path[512];
    wchar_t addr_w[24];
    HKEY key = NULL;
    SONATINA_PROFILE prof;
    DWORD aac_mode = 1;
    wchar_t codec_w[32];
    DWORD type = 0;
    DWORD sz = sizeof(codec_w);
    int i;
    int n;

    if (out == NULL || addr_hex == NULL) {
        return 0;
    }
    for (i = 0; i < 12 && addr_hex[i] != '\0'; i++) {
        addr_w[i] = (wchar_t)addr_hex[i];
    }
    addr_w[i] = L'\0';

    n = _snwprintf(path, sizeof(path) / sizeof(path[0]), L"%ls\\Devices\\%ls", root, addr_w);
    if (n < 0 || (size_t)n >= sizeof(path) / sizeof(path[0])) {
        return 0;
    }
    if (RegOpenKeyExW(hive, path, 0, KEY_READ, &key) != ERROR_SUCCESS) {
        return 0;
    }

    sont_init_default_profile(&prof);
    if (RegQueryValueExW(key, L"Codec", NULL, &type, (LPBYTE)codec_w, &sz) == ERROR_SUCCESS &&
        type == REG_SZ && sz >= sizeof(wchar_t)) {
        codec_w[(sizeof(codec_w) / sizeof(codec_w[0])) - 1] = L'\0';
        if (_wcsicmp(codec_w, L"pcm") == 0) {
            prof.Codec = SonatinaCodecPcm;
        } else if (_wcsicmp(codec_w, L"sbc") == 0) {
            prof.Codec = SonatinaCodecSbc;
        } else if (_wcsicmp(codec_w, L"aac") == 0) {
            prof.Codec = SonatinaCodecAac;
        } else if (_wcsicmp(codec_w, L"aptx") == 0) {
            prof.Codec = SonatinaCodecAptx;
        } else if (_wcsicmp(codec_w, L"ldac") == 0) {
            prof.Codec = SonatinaCodecLdac;
        }
    }

    sont_read_dword(key, L"SbcSamplingFrequency", &prof.SbcSampling);
    sont_read_dword(key, L"SbcChannelMode", &prof.SbcChannelMode);
    sont_read_dword(key, L"SbcBlockLength", &prof.SbcBlocks);
    sont_read_dword(key, L"SbcSubbands", &prof.SbcSubbands);
    sont_read_dword(key, L"SbcAllocationMethod", &prof.SbcAllocation);
    sont_read_dword(key, L"SbcMinimumBitpool", &prof.SbcMinBitpool);
    sont_read_dword(key, L"SbcMaximumBitpool", &prof.SbcMaxBitpool);
    sont_read_dword(key, L"AacSamplingFrequency", &prof.AacSampling);
    sont_read_dword(key, L"AacChannelMode", &aac_mode);
    prof.AacChannels = (aac_mode == 0) ? 1u : 2u;
    sont_read_dword(key, L"AacBitrate", &prof.AacBitrate);
    RegCloseKey(key);

    if (!SontProfileValidateFields(prof.Codec,
                                   prof.SbcSampling,
                                   prof.SbcChannelMode,
                                   prof.SbcBlocks,
                                   prof.SbcSubbands,
                                   prof.SbcAllocation,
                                   prof.SbcMinBitpool,
                                   prof.SbcMaxBitpool,
                                   prof.AacSampling,
                                   prof.AacChannels,
                                   prof.AacBitrate)) {
        return 0;
    }

    *out = prof;
    return 1;
}

LONG SontSvcWriteCurrent(HKEY hive,
                         const wchar_t *root,
                         const char *addr_hex,
                         const SONATINA_STATE *state)
{
    wchar_t path[512];
    wchar_t addr_w[24];
    wchar_t updated[40];
    SYSTEMTIME st;
    HKEY key = NULL;
    const wchar_t *codec_w;
    LONG rc;
    int i;
    int n;

    if (state == NULL || addr_hex == NULL) {
        return ERROR_INVALID_PARAMETER;
    }
    for (i = 0; i < 12 && addr_hex[i] != '\0'; i++) {
        addr_w[i] = (wchar_t)addr_hex[i];
    }
    addr_w[i] = L'\0';

    n = _snwprintf(path, sizeof(path) / sizeof(path[0]),
                   L"%ls\\Devices\\%ls\\Current", root, addr_w);
    if (n < 0 || (size_t)n >= sizeof(path) / sizeof(path[0])) {
        return ERROR_BUFFER_OVERFLOW;
    }
    rc = RegCreateKeyExW(hive, path, 0, NULL, REG_OPTION_NON_VOLATILE,
                         KEY_WRITE, NULL, &key, NULL);
    if (rc != ERROR_SUCCESS) {
        return rc;
    }

    codec_w = sont_codec_name_w(state->Codec);
    (void)RegSetValueExW(key, L"Codec", 0, REG_SZ,
                         (const BYTE *)codec_w,
                         (DWORD)((wcslen(codec_w) + 1) * sizeof(wchar_t)));
    (void)RegSetValueExW(key, L"Bitrate", 0, REG_DWORD,
                         (const BYTE *)&state->BitrateKbps, sizeof(DWORD));
    (void)RegSetValueExW(key, L"Packets", 0, REG_DWORD,
                         (const BYTE *)&state->Packets, sizeof(DWORD));
    (void)RegSetValueExW(key, L"Errors", 0, REG_DWORD,
                         (const BYTE *)&state->Errors, sizeof(DWORD));
    (void)RegSetValueExW(key, L"TxTimeUs", 0, REG_DWORD,
                         (const BYTE *)&state->TxTimeUs, sizeof(DWORD));
    (void)RegSetValueExW(key, L"DelayMs", 0, REG_DWORD,
                         (const BYTE *)&state->DelayMs, sizeof(DWORD));
    (void)RegSetValueExW(key, L"Opened", 0, REG_DWORD,
                         (const BYTE *)&state->Opened, sizeof(DWORD));
    (void)RegSetValueExW(key, L"Bitpool", 0, REG_DWORD,
                         (const BYTE *)&state->CurrentBitpool, sizeof(DWORD));
    (void)RegSetValueExW(key, L"TxTimeMaxUs", 0, REG_DWORD,
                         (const BYTE *)&state->TxTimeMaxUs, sizeof(DWORD));
    (void)RegSetValueExW(key, L"TxTimeAvgUs", 0, REG_DWORD,
                         (const BYTE *)&state->TxTimeAvgUs, sizeof(DWORD));
    (void)RegSetValueExW(key, L"TxTimeHighCount", 0, REG_DWORD,
                         (const BYTE *)&state->TxTimeHighCount, sizeof(DWORD));
    (void)RegSetValueExW(key, L"TxTimeCriticalCount", 0, REG_DWORD,
                         (const BYTE *)&state->TxTimeCriticalCount, sizeof(DWORD));
    (void)RegSetValueExW(key, L"AbrAdjustments", 0, REG_DWORD,
                         (const BYTE *)&state->AbrAdjustments, sizeof(DWORD));
    (void)RegSetValueExW(key, L"TxFlushTO", 0, REG_DWORD,
                         (const BYTE *)&state->TxFlushTO, sizeof(DWORD));

    GetSystemTime(&st);
    _snwprintf(updated, sizeof(updated) / sizeof(updated[0]),
               L"%04u-%02u-%02uT%02u:%02u:%02u+00:00",
               (unsigned)st.wYear, (unsigned)st.wMonth, (unsigned)st.wDay,
               (unsigned)st.wHour, (unsigned)st.wMinute, (unsigned)st.wSecond);
    updated[(sizeof(updated) / sizeof(updated[0])) - 1] = L'\0';
    (void)RegSetValueExW(key, L"UpdatedAt", 0, REG_SZ,
                         (const BYTE *)updated,
                         (DWORD)((wcslen(updated) + 1) * sizeof(wchar_t)));

    RegCloseKey(key);
    return ERROR_SUCCESS;
}
