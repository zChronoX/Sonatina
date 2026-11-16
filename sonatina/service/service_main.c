/* SonatinaSvc.exe - Native Win32 service for SonatinaAudio.sys.
 *
 * Watches HKLM\SYSTEM\CurrentControlSet\Services\SonatinaAudio\Parameters\Control
 * (or a custom hive/root in --console/--once mode) using RegNotifyChangeKeyValue,
 * coalesces bursts with SontPolicy (debounce + 20 s cooldown), pushes validated
 * profiles via IOCTL_SONATINA_SET_PROFILE + IOCTL_SONATINA_RECONNECT, cycles
 * the matching AVRCP devnode, and syncs Current telemetry from IOCTL_SONATINA_GET_STATE.
 */
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <shellapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "apply_policy.h"
#include "device_ctrl.h"
#include "reg_store.h"

#define SONT_SVC_NAME L"SonatinaSvc"
#define SONT_DEFAULT_DEBOUNCE_MS 1000ULL
#define SONT_DEFAULT_COOLDOWN_MS 20000ULL
#define SONT_WAIT_TIMEOUT_MS     5000U
#define SONT_MAX_DEVICES         32U

typedef struct _SONT_SVC_CONFIG {
    HKEY hive;
    const wchar_t *hive_name;
    wchar_t root[512];
    uint64_t debounce_ms;
    uint64_t cooldown_ms;
    DWORD duration_ms;
    int console_mode;
    int once_mode;
    int no_avrcp;
} SONT_SVC_CONFIG;

static SONT_SVC_CONFIG g_cfg;
static SERVICE_STATUS g_svc_status;
static SERVICE_STATUS_HANDLE g_svc_handle = NULL;
static HANDLE g_stop_event = NULL;
static unsigned long g_apply_count = 0;

static void sont_log(const char *msg)
{
    SYSTEMTIME st;
    GetLocalTime(&st);
    if (g_cfg.console_mode || g_cfg.once_mode) {
        printf("[%02u:%02u:%02u.%03u] %s\n",
               (unsigned)st.wHour, (unsigned)st.wMinute, (unsigned)st.wSecond,
               (unsigned)st.wMilliseconds, msg);
        fflush(stdout);
    } else {
        char buf[300];
        _snprintf(buf, sizeof(buf), "SonatinaSvc: %s\n", msg);
        buf[sizeof(buf) - 1] = '\0';
        OutputDebugStringA(buf);
    }
}

static BOOL WINAPI sont_console_ctrl(DWORD ctrl_type)
{
    (void)ctrl_type;
    if (g_stop_event != NULL) {
        SetEvent(g_stop_event);
    }
    return TRUE;
}

static void sont_set_service_state(DWORD state, DWORD exit_code, DWORD wait_hint)
{
    static DWORD checkpoint = 1;
    if (g_svc_handle == NULL) {
        return;
    }
    g_svc_status.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
    g_svc_status.dwCurrentState = state;
    g_svc_status.dwWin32ExitCode = exit_code;
    g_svc_status.dwWaitHint = wait_hint;
    if (state == SERVICE_START_PENDING) {
        g_svc_status.dwControlsAccepted = 0;
    } else {
        g_svc_status.dwControlsAccepted = SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN;
    }
    if (state == SERVICE_RUNNING || state == SERVICE_STOPPED) {
        g_svc_status.dwCheckPoint = 0;
    } else {
        g_svc_status.dwCheckPoint = checkpoint++;
    }
    SetServiceStatus(g_svc_handle, &g_svc_status);
}

static DWORD WINAPI sont_svc_ctrl_handler(DWORD control,
                                          DWORD event_type,
                                          LPVOID event_data,
                                          LPVOID context)
{
    (void)event_type;
    (void)event_data;
    (void)context;

    switch (control) {
    case SERVICE_CONTROL_STOP:
    case SERVICE_CONTROL_SHUTDOWN:
        sont_set_service_state(SERVICE_STOP_PENDING, NO_ERROR, 5000);
        if (g_stop_event != NULL) {
            SetEvent(g_stop_event);
        }
        return NO_ERROR;
    case SERVICE_CONTROL_INTERROGATE:
        return NO_ERROR;
    default:
        return ERROR_CALL_NOT_IMPLEMENTED;
    }
}

static void sont_apply_profiles(void)
{
    SONT_DEVICE_ADDR addrs[SONT_MAX_DEVICES];
    size_t count = 0;
    size_t i;
    size_t updated = 0;
    LONG rc;

    g_apply_count++;
    rc = SontSvcListDevices(g_cfg.hive, g_cfg.root, addrs, SONT_MAX_DEVICES, &count);
    if (rc != ERROR_SUCCESS || count == 0) {
        char msg[160];
        _snprintf(msg, sizeof(msg), "apply #%lu: no profiles found (rc=%ld)",
                  g_apply_count, rc);
        msg[sizeof(msg) - 1] = '\0';
        sont_log(msg);
        (void)SontSvcPollDriverStates(g_cfg.hive, g_cfg.root, &updated);
        return;
    }

    for (i = 0; i < count; i++) {
        SONATINA_PROFILE prof;
        int matched = 0;
        int cycled = 0;
        char msg[200];

        if (!SontSvcReadProfile(g_cfg.hive, g_cfg.root, addrs[i].hex, &prof)) {
            _snprintf(msg, sizeof(msg), "apply #%lu: invalid/missing profile for %s",
                      g_apply_count, addrs[i].hex);
            msg[sizeof(msg) - 1] = '\0';
            sont_log(msg);
            continue;
        }

        (void)SontSvcPushProfileToDriver(addrs[i].hex, &prof, 1, &matched, sont_log);
        if (!g_cfg.no_avrcp) {
            (void)SontSvcCycleAvrcpDevnode(addrs[i].hex, &cycled, sont_log);
        }

        _snprintf(msg, sizeof(msg),
                  "apply #%lu: device=%s codec=%lu sbc=%luHz bp=%lu..%lu driver_matched=%d avrcp_cycled=%d",
                  g_apply_count, addrs[i].hex,
                  (unsigned long)prof.Codec,
                  (unsigned long)prof.SbcSampling,
                  (unsigned long)prof.SbcMinBitpool,
                  (unsigned long)prof.SbcMaxBitpool,
                  matched, cycled);
        msg[sizeof(msg) - 1] = '\0';
        sont_log(msg);
    }

    (void)SontSvcPollDriverStates(g_cfg.hive, g_cfg.root, &updated);
}

static LONG sont_arm_watch(HKEY key, HANDLE reg_event)
{
    return RegNotifyChangeKeyValue(
        key,
        FALSE,
        REG_NOTIFY_CHANGE_LAST_SET | REG_NOTIFY_THREAD_AGNOSTIC,
        reg_event,
        TRUE);
}

static DWORD sont_run_watcher_loop(void)
{
    HKEY watch_key = NULL;
    HANDLE reg_event = NULL;
    HANDLE timer = NULL;
    HANDLE handles[3];
    SONT_APPLY_POLICY policy;
    uint64_t start_ms;
    LONG rc;

    rc = SontSvcEnsureRegistry(g_cfg.hive, g_cfg.root);
    if (rc != ERROR_SUCCESS) {
        char msg[160];
        _snprintf(msg, sizeof(msg), "SontSvcEnsureRegistry failed (%ld)", rc);
        msg[sizeof(msg) - 1] = '\0';
        sont_log(msg);
        return (DWORD)rc;
    }

    if (g_cfg.once_mode) {
        sont_apply_profiles();
        return ERROR_SUCCESS;
    }

    rc = SontSvcOpenControlKey(g_cfg.hive, g_cfg.root, KEY_NOTIFY | KEY_READ, &watch_key);
    if (rc != ERROR_SUCCESS) {
        char msg[160];
        _snprintf(msg, sizeof(msg), "SontSvcOpenControlKey failed (%ld)", rc);
        msg[sizeof(msg) - 1] = '\0';
        sont_log(msg);
        return (DWORD)rc;
    }

    reg_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    timer = CreateWaitableTimerW(NULL, FALSE, NULL);
    if (reg_event == NULL || timer == NULL) {
        DWORD err = GetLastError();
        if (reg_event != NULL) CloseHandle(reg_event);
        if (timer != NULL) CloseHandle(timer);
        RegCloseKey(watch_key);
        return err;
    }

    SontPolicyInit(&policy, g_cfg.debounce_ms, g_cfg.cooldown_ms);
    ResetEvent(reg_event);
    rc = sont_arm_watch(watch_key, reg_event);
    if (rc != ERROR_SUCCESS) {
        CloseHandle(reg_event);
        CloseHandle(timer);
        RegCloseKey(watch_key);
        return (DWORD)rc;
    }

    handles[0] = g_stop_event;
    handles[1] = reg_event;
    handles[2] = timer;
    start_ms = GetTickCount64();

    {
        char msg[200];
        _snprintf(msg, sizeof(msg),
                  "watching Control key (debounce=%llu ms cooldown=%llu ms)",
                  (unsigned long long)g_cfg.debounce_ms,
                  (unsigned long long)g_cfg.cooldown_ms);
        msg[sizeof(msg) - 1] = '\0';
        sont_log(msg);
    }

    for (;;) {
        DWORD wait_ms = SONT_WAIT_TIMEOUT_MS;
        DWORD wait_rc;

        if (g_cfg.duration_ms > 0) {
            uint64_t elapsed = GetTickCount64() - start_ms;
            if (elapsed >= g_cfg.duration_ms) {
                break;
            }
            if ((uint64_t)wait_ms > (uint64_t)g_cfg.duration_ms - elapsed) {
                wait_ms = (DWORD)((uint64_t)g_cfg.duration_ms - elapsed);
            }
        }

        wait_rc = WaitForMultipleObjects(3, handles, FALSE, wait_ms);
        if (wait_rc == WAIT_OBJECT_0) {
            break;
        }
        if (wait_rc == WAIT_OBJECT_0 + 1) {
            LARGE_INTEGER due;
            uint64_t delay_ms = (g_cfg.debounce_ms > 0) ? g_cfg.debounce_ms : 1ULL;

            ResetEvent(reg_event);
            rc = sont_arm_watch(watch_key, reg_event);
            if (rc != ERROR_SUCCESS) {
                break;
            }
            SontPolicyNotify(&policy, GetTickCount64());
            sont_log("registry changed, debounce armed");
            due.QuadPart = -(LONGLONG)(delay_ms * 10000ULL);
            SetWaitableTimer(timer, &due, 0, NULL, NULL, FALSE);
            continue;
        }
        if (wait_rc == WAIT_OBJECT_0 + 2) {
            SONT_POLICY_OUTCOME outcome = SontPolicyPoll(&policy, GetTickCount64());
            if (outcome == SONT_POLICY_APPLY) {
                sont_apply_profiles();
            } else if (outcome == SONT_POLICY_COOLDOWN) {
                uint64_t elapsed_ms = 0;
                char msg[160];
                (void)SontPolicyElapsedSinceApplyMs(&policy, GetTickCount64(), &elapsed_ms);
                _snprintf(msg, sizeof(msg),
                          "cooldown, skip (last apply %llu ms ago, cooldown %llu ms)",
                          (unsigned long long)elapsed_ms,
                          (unsigned long long)g_cfg.cooldown_ms);
                msg[sizeof(msg) - 1] = '\0';
                sont_log(msg);
            }
            continue;
        }
        if (wait_rc == WAIT_TIMEOUT) {
            size_t updated = 0;
            (void)SontSvcPollDriverStates(g_cfg.hive, g_cfg.root, &updated);
            continue;
        }
        break;
    }

    CloseHandle(timer);
    CloseHandle(reg_event);
    RegCloseKey(watch_key);
    return ERROR_SUCCESS;
}

static void WINAPI sont_service_main(DWORD argc, LPWSTR *argv)
{
    DWORD rc;
    (void)argc;
    (void)argv;

    g_svc_handle = RegisterServiceCtrlHandlerExW(SONT_SVC_NAME, sont_svc_ctrl_handler, NULL);
    if (g_svc_handle == NULL) {
        return;
    }
    sont_set_service_state(SERVICE_START_PENDING, NO_ERROR, 3000);

    g_stop_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (g_stop_event == NULL) {
        sont_set_service_state(SERVICE_STOPPED, GetLastError(), 0);
        return;
    }

    sont_set_service_state(SERVICE_RUNNING, NO_ERROR, 0);
    rc = sont_run_watcher_loop();
    CloseHandle(g_stop_event);
    g_stop_event = NULL;
    sont_set_service_state(SERVICE_STOPPED, rc, 0);
}

static void sont_init_config(void)
{
    ZeroMemory(&g_cfg, sizeof(g_cfg));
    g_cfg.hive = HKEY_LOCAL_MACHINE;
    g_cfg.hive_name = L"HKLM";
    wcsncpy(g_cfg.root, SONT_DEFAULT_REG_ROOT,
            (sizeof(g_cfg.root) / sizeof(g_cfg.root[0])) - 1);
    g_cfg.debounce_ms = SONT_DEFAULT_DEBOUNCE_MS;
    g_cfg.cooldown_ms = SONT_DEFAULT_COOLDOWN_MS;
}

static int sont_parse_args(int argc, LPWSTR *argv)
{
    int i;
    for (i = 1; i < argc; i++) {
        if (wcscmp(argv[i], L"--console") == 0) {
            g_cfg.console_mode = 1;
        } else if (wcscmp(argv[i], L"--once") == 0) {
            g_cfg.once_mode = 1;
        } else if (wcscmp(argv[i], L"--no-avrcp") == 0) {
            g_cfg.no_avrcp = 1;
        } else if (wcscmp(argv[i], L"--hive") == 0 && i + 1 < argc) {
            i++;
            if (_wcsicmp(argv[i], L"HKCU") == 0) {
                g_cfg.hive = HKEY_CURRENT_USER;
                g_cfg.hive_name = L"HKCU";
            } else if (_wcsicmp(argv[i], L"HKLM") == 0) {
                g_cfg.hive = HKEY_LOCAL_MACHINE;
                g_cfg.hive_name = L"HKLM";
            } else {
                fprintf(stderr, "error: --hive must be HKCU or HKLM\n");
                return 2;
            }
        } else if (wcscmp(argv[i], L"--root") == 0 && i + 1 < argc) {
            i++;
            wcsncpy(g_cfg.root, argv[i],
                    (sizeof(g_cfg.root) / sizeof(g_cfg.root[0])) - 1);
            g_cfg.root[(sizeof(g_cfg.root) / sizeof(g_cfg.root[0])) - 1] = L'\0';
        } else if (wcscmp(argv[i], L"--debounce") == 0 && i + 1 < argc) {
            g_cfg.debounce_ms = (uint64_t)_wcstoui64(argv[++i], NULL, 10);
        } else if (wcscmp(argv[i], L"--cooldown") == 0 && i + 1 < argc) {
            g_cfg.cooldown_ms = (uint64_t)_wcstoui64(argv[++i], NULL, 10);
        } else if (wcscmp(argv[i], L"--duration") == 0 && i + 1 < argc) {
            double sec = _wtof(argv[++i]);
            g_cfg.duration_ms = (sec > 0.0) ? (DWORD)(sec * 1000.0) : 0U;
        } else if (wcscmp(argv[i], L"--help") == 0 || wcscmp(argv[i], L"-h") == 0) {
            printf("Usage: SonatinaSvc.exe [--console | --once] [--hive HKLM|HKCU] "
                   "[--root PATH] [--debounce MS] [--cooldown MS] [--duration SEC] "
                   "[--no-avrcp]\n");
            return 1;
        } else {
            fprintf(stderr, "error: unknown option\n");
            return 2;
        }
    }
    return 0;
}

int main(void)
{
    int argc = 0;
    LPWSTR *argv;
    int parse_rc;

    sont_init_config();
    argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (argv == NULL) {
        return 2;
    }
    parse_rc = sont_parse_args(argc, argv);
    LocalFree(argv);
    if (parse_rc != 0) {
        return (parse_rc == 1) ? 0 : parse_rc;
    }

    if (g_cfg.console_mode || g_cfg.once_mode) {
        DWORD rc;
        g_stop_event = CreateEventW(NULL, TRUE, FALSE, NULL);
        if (g_stop_event == NULL) {
            return 1;
        }
        SetConsoleCtrlHandler(sont_console_ctrl, TRUE);
        rc = sont_run_watcher_loop();
        CloseHandle(g_stop_event);
        g_stop_event = NULL;
        return (rc == ERROR_SUCCESS) ? 0 : 1;
    }

    {
        SERVICE_TABLE_ENTRYW table[] = {
            { (LPWSTR)SONT_SVC_NAME, sont_service_main },
            { NULL, NULL }
        };
        if (!StartServiceCtrlDispatcherW(table)) {
            DWORD err = GetLastError();
            if (err == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT) {
                fprintf(stderr,
                        "SonatinaSvc must be started by the SCM, or run with --console / --once.\n");
            }
            return 1;
        }
    }
    return 0;
}
