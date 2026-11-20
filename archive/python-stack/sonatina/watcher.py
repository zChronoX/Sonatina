"""Settings watcher: registry notification -> debounce -> cooldown -> apply.

The service reacts to configuration changes as follows:

* ``RegNotifyChangeKeyValue(..., REG_NOTIFY_CHANGE_LAST_SET |
  REG_NOTIFY_THREAD_AGNOSTIC, event, TRUE)`` (asynchronous, one-shot);
* ``WaitForMultipleObjects`` on [stop, registry, timer] with a 5000 ms timeout;
* a debounce timer for bursts of changes;
* a 20 000 ms cooldown between two actions.

``ApplyPolicy`` is pure Python and injectable, so the timing logic is unit
tested without touching the registry.
"""

from __future__ import annotations

import ctypes
import threading
import time
import winreg
from ctypes import wintypes as wt

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102

REG_NOTIFY_CHANGE_LAST_SET = 0x00000004
REG_NOTIFY_THREAD_AGNOSTIC = 0x10000000
NOTIFY_FLAGS = REG_NOTIFY_CHANGE_LAST_SET | REG_NOTIFY_THREAD_AGNOSTIC

HIVES = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

kernel32.CreateEventW.argtypes = [wt.LPVOID, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.SetEvent.argtypes = [wt.HANDLE]
kernel32.SetEvent.restype = wt.BOOL
kernel32.ResetEvent.argtypes = [wt.HANDLE]
kernel32.ResetEvent.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CreateWaitableTimerW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
kernel32.CreateWaitableTimerW.restype = wt.HANDLE
kernel32.SetWaitableTimer.argtypes = [
    wt.HANDLE,
    ctypes.POINTER(ctypes.c_longlong),
    wt.LONG,
    wt.LPVOID,
    wt.LPVOID,
    wt.BOOL,
]
kernel32.SetWaitableTimer.restype = wt.BOOL
kernel32.WaitForMultipleObjects.argtypes = [
    wt.DWORD,
    ctypes.POINTER(wt.HANDLE),
    wt.BOOL,
    wt.DWORD,
]
kernel32.WaitForMultipleObjects.restype = wt.DWORD

advapi32.RegNotifyChangeKeyValue.argtypes = [
    wt.HANDLE,
    wt.BOOL,
    wt.DWORD,
    wt.HANDLE,
    wt.BOOL,
]
advapi32.RegNotifyChangeKeyValue.restype = wt.LONG


def default_log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print("[%s.%03d] %s" % (stamp, int(time.time() * 1000) % 1000, message), flush=True)


class Win32Error(RuntimeError):
    pass


def _check(ok, what: str) -> None:
    if not ok:
        raise Win32Error(
            "%s failed: %s" % (what, ctypes.WinError(ctypes.get_last_error()))
        )


class ApplyPolicy:
    """Debounce and cooldown decision logic.

    ``poll()`` returns ``None`` (nothing to do), ``"apply"`` (act now) or
    ``"cooldown"`` (the debounce expired but the previous apply is too recent).
    """

    def __init__(
        self, debounce_ms: int = 1000, cooldown_ms: int = 20000, clock=time.monotonic
    ) -> None:
        self.debounce_ms = max(0, int(debounce_ms))
        self.cooldown_ms = max(0, int(cooldown_ms))
        self.clock = clock
        self._pending = False
        self._due = 0.0
        self._last_apply: float | None = None

    def notify(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        self._pending = True
        self._due = now + self.debounce_ms / 1000.0

    @property
    def pending(self) -> bool:
        return self._pending

    def poll(self, now: float | None = None) -> str | None:
        now = self.clock() if now is None else now
        if not self._pending or now < self._due:
            return None
        self._pending = False
        if (
            self._last_apply is not None
            and (now - self._last_apply) * 1000.0 < self.cooldown_ms
        ):
            return "cooldown"
        self._last_apply = now
        return "apply"

    def elapsed_since_apply(self, now: float | None = None) -> float | None:
        if self._last_apply is None:
            return None
        now = self.clock() if now is None else now
        return now - self._last_apply


class SettingsWatcher:
    def __init__(
        self,
        key_path: str,
        hive: str = "HKCU",
        debounce_ms: int = 1000,
        cooldown_ms: int = 20000,
        timeout_ms: int = 5000,
        log=default_log,
        clock=time.monotonic,
    ) -> None:
        if hive not in HIVES:
            raise ValueError("hive must be HKCU or HKLM")
        self.key_path = key_path
        self.hive = HIVES[hive]
        self.hive_name = hive
        self.debounce_ms = debounce_ms
        self.cooldown_ms = cooldown_ms
        self.timeout_ms = timeout_ms
        self.log = log
        self.policy = ApplyPolicy(debounce_ms, cooldown_ms, clock)
        self._stop = None
        self._lock = threading.Lock()

    def stop(self) -> None:
        with self._lock:
            if self._stop:
                kernel32.SetEvent(self._stop)

    def _create_handles(self):
        stop = kernel32.CreateEventW(None, True, False, None)
        if not stop:
            raise Win32Error(
                "CreateEventW failed: %s" % ctypes.WinError(ctypes.get_last_error())
            )
        reg = kernel32.CreateEventW(None, True, False, None)
        timer = kernel32.CreateWaitableTimerW(None, False, None)
        if not reg or not timer:
            for handle in (stop, reg, timer):
                if handle:
                    kernel32.CloseHandle(handle)
            raise Win32Error(
                "cannot create watcher handles: %s"
                % ctypes.WinError(ctypes.get_last_error())
            )
        return stop, reg, timer

    @staticmethod
    def _arm(key_handle: int, event) -> None:
        ok = advapi32.RegNotifyChangeKeyValue(
            wt.HANDLE(key_handle), True, NOTIFY_FLAGS, event, True
        )
        if ok != 0:
            raise Win32Error(
                "RegNotifyChangeKeyValue failed: %s"
                % ctypes.WinError(ctypes.get_last_error())
            )

    @staticmethod
    def _due_in(ms: int) -> ctypes.c_longlong:
        # Negative value = relative time, in 100 ns units.
        return ctypes.c_longlong(-int(max(1, ms)) * 10000)

    def run(self, on_apply) -> None:
        try:
            key = winreg.OpenKey(
                self.hive, self.key_path, 0, winreg.KEY_NOTIFY | winreg.KEY_READ
            )
        except OSError as exc:
            raise Win32Error(
                "cannot open %s\\%s for notification: %s"
                % (self.hive_name, self.key_path, exc)
            )
        stop, reg, timer = self._create_handles()
        with self._lock:
            self._stop = stop
        handles = (wt.HANDLE * 3)(stop, reg, timer)
        self.log(
            "watching %s\\%s debounce=%dms cooldown=%dms timeout=%dms"
            % (
                self.hive_name,
                self.key_path,
                self.debounce_ms,
                self.cooldown_ms,
                self.timeout_ms,
            )
        )
        try:
            kernel32.ResetEvent(reg)
            self._arm(key.handle, reg)
            while True:
                rc = kernel32.WaitForMultipleObjects(3, handles, False, self.timeout_ms)
                if rc == WAIT_OBJECT_0:
                    break
                if rc == WAIT_OBJECT_0 + 1:
                    kernel32.ResetEvent(reg)
                    self._arm(key.handle, reg)
                    self.policy.notify()
                    self.log("registry changed, debounce armed")
                    due = self._due_in(self.debounce_ms)
                    kernel32.SetWaitableTimer(
                        timer, ctypes.byref(due), 0, None, None, False
                    )
                    continue
                if rc == WAIT_OBJECT_0 + 2:
                    outcome = self.policy.poll()
                    if outcome == "apply":
                        self.log("apply")
                        on_apply()
                    elif outcome == "cooldown":
                        elapsed = self.policy.elapsed_since_apply()
                        self.log(
                            "cooldown, skip (last apply %.1fs ago, cooldown %dms)"
                            % (elapsed or 0.0, self.cooldown_ms)
                        )
                    continue
                if rc == WAIT_TIMEOUT:
                    continue
                raise Win32Error(
                    "WaitForMultipleObjects failed: %s"
                    % ctypes.WinError(ctypes.get_last_error())
                )
        finally:
            with self._lock:
                self._stop = None
            for handle in (stop, reg, timer):
                if handle:
                    kernel32.CloseHandle(handle)
            winreg.CloseKey(key)
