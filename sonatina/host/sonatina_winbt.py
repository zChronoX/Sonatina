"""Windows' own Bluetooth, seen from Sonatina (N2, 1.3.0).

Headphones remember the last device they were connected to. When that is the PC's
built-in Bluetooth (the same headphones paired there too), they connect to Windows at
power-on and Sonatina's pages time out for as long as they stay there: 2026-11-22, 17
minutes of PAGE_TIMEOUT with the CMF connected to Windows a metre away.

After a few failed pages Sonatina asks Windows (WinRT Windows.Devices.Bluetooth) whether
it has a link to the same address; if so the Home shows a banner and the tray a balloon.
Nothing is changed in Windows: a third-party app cannot disconnect an audio device there,
the user does it in Settings (the banner opens them).
"""
from __future__ import annotations

import asyncio
import logging
import sys

log = logging.getLogger("sonatina")

CHECK_AFTER_FAILURES = 3      # page timeouts in a row before asking Windows
CHECK_EVERY_S = 30.0          # then at most once every 30 s
SETTINGS_URI = "ms-settings:bluetooth"


def _apartment() -> None:
    """WinRT calls from a worker thread need a COM apartment (as in sonatina_media)."""
    try:
        import winrt.runtime
        winrt.runtime.init_apartment(winrt.runtime.MTA)
    except Exception:
        pass                       # already initialised on this thread


def address_to_int(address: str) -> int:
    return int(address.replace(":", "").replace("-", ""), 16)


def _windows_connected(address: str) -> bool | None:
    """True/False: Windows has / has not a link to `address`; None: cannot tell."""
    if sys.platform != "win32":
        return None
    try:
        from winrt.windows.devices.bluetooth import BluetoothConnectionStatus, BluetoothDevice
        from winrt.windows.foundation import AsyncStatus
    except Exception as error:
        log.debug("Windows Bluetooth API unavailable: %r", error)
        return None
    _apartment()
    try:
        operation = BluetoothDevice.from_bluetooth_address_async(address_to_int(address))
        if operation.wait(3.0) != AsyncStatus.COMPLETED:
            return None
        device = operation.get_results()
        if device is None:            # never paired with Windows
            return False
        try:
            return device.connection_status == BluetoothConnectionStatus.CONNECTED
        finally:
            try:
                device.close()
            except Exception:
                pass
    except Exception as error:      # no built-in radio, radio off, ...
        log.debug("Windows Bluetooth check: %r", error)
        return None


class WindowsLinkCheck:
    """Asked by keep_connected after failed pages; `conflict` is shown by the UI."""

    def __init__(self, probe=None, clock=None) -> None:
        import time
        self.probe = probe or _windows_connected
        self.clock = clock or time.monotonic
        self.conflict: str | None = None     # address Windows holds, while it does
        self.next_check = 0.0
        self.on_found = None                 # callback(address), once per episode

    def reset(self) -> None:
        """Sonatina connected (or the preferred headset changed): the banner goes."""
        self.conflict = None
        self.next_check = 0.0

    async def after_failure(self, address: str, failures: int, timeout: bool) -> None:
        if not timeout or failures < CHECK_AFTER_FAILURES:
            return
        now = self.clock()
        if now < self.next_check:
            return
        self.next_check = now + CHECK_EVERY_S
        held = await asyncio.to_thread(self.probe, address)
        if held is None:
            return
        if held and self.conflict != address:
            self.conflict = address
            log.warning("%s is connected to Windows' own Bluetooth: Sonatina cannot reach it", address)
            if self.on_found is not None:
                self.on_found(address)
        elif not held and self.conflict is not None:
            log.info("%s no longer connected to Windows", address)
            self.conflict = None


def windows_has_bluetooth() -> bool | None:
    """Has Windows a Bluetooth radio of its own (for mice, keyboards...)? The setup guide
    (M6) shows it; None when it cannot tell."""
    if sys.platform != "win32":
        return None
    _apartment()
    try:
        from winrt.windows.devices.bluetooth import BluetoothAdapter
        from winrt.windows.foundation import AsyncStatus
        operation = BluetoothAdapter.get_default_async()
        if operation.wait(3.0) != AsyncStatus.COMPLETED:
            return None
        return operation.get_results() is not None
    except Exception as error:
        log.debug("Windows Bluetooth adapter: %r", error)
        return None


def open_bluetooth_settings() -> None:
    if sys.platform == "win32":
        import os
        os.startfile(SETTINGS_URI)   # noqa: S606 - a fixed ms-settings URI
