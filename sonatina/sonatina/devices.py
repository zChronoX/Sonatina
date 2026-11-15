"""Enumerate the Bluetooth audio devnodes exposed under the BTHENUM branch."""

from __future__ import annotations

import re
import winreg
from dataclasses import dataclass

from .bdaddr import from_instance_id

BTHENUM_PATH = r"SYSTEM\CurrentControlSet\Enum\BTHENUM"
SINK_SERVICE = "{0000110b-0000-1000-8000-00805f9b34fb}"

_FRIENDLY = re.compile(r"\(([^()]*)\)\s*$")


def friendly_name(raw: str | None) -> str:
    """Extract a readable name from values like '@btav.sys,#1;%1%0;(SOUNDPEATS Air5)'."""
    if not raw:
        return ""
    match = _FRIENDLY.search(raw)
    if match:
        return match.group(1).strip()
    return raw.strip()


@dataclass
class Device:
    address: str
    name: str = ""
    service: str = ""
    driver: str = ""
    instance: str = ""

    def summary(self) -> str:
        return "%-12s  %-24s  service=%-12s driver=%s" % (
            self.address,
            self.name or "(unknown)",
            self.service or "-",
            self.driver or "-",
        )


def _string_value(key, name: str) -> str:
    try:
        value = winreg.QueryValueEx(key, name)[0]
        return value if isinstance(value, str) else str(value)
    except OSError:
        return ""


def enumerate_sinks() -> list[Device]:
    """Return the Bluetooth audio sink devnodes present under HKLM\\...\\Enum\\BTHENUM."""
    devices: dict[str, Device] = {}
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, BTHENUM_PATH)
    except OSError:
        return []
    try:
        index = 0
        while True:
            try:
                service_key_name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if not service_key_name.lower().startswith(SINK_SERVICE):
                continue
            try:
                service_key = winreg.OpenKey(root, service_key_name)
            except OSError:
                continue
            try:
                child = 0
                while True:
                    try:
                        instance = winreg.EnumKey(service_key, child)
                    except OSError:
                        break
                    child += 1
                    address = from_instance_id(instance)
                    if not address:
                        continue
                    try:
                        dev_key = winreg.OpenKey(service_key, instance)
                    except OSError:
                        continue
                    try:
                        name = friendly_name(_string_value(dev_key, "FriendlyName"))
                        if not name:
                            name = friendly_name(_string_value(dev_key, "DeviceDesc"))
                        candidate = Device(
                            address=address,
                            name=name,
                            service=_string_value(dev_key, "Service"),
                            driver=_string_value(dev_key, "Driver"),
                            instance=instance,
                        )
                    finally:
                        winreg.CloseKey(dev_key)
                    current = devices.get(address)
                    if current is None or (not current.name and candidate.name):
                        devices[address] = candidate
            finally:
                winreg.CloseKey(service_key)
    finally:
        winreg.CloseKey(root)
    return [devices[address] for address in sorted(devices)]


def find(address: str) -> Device | None:
    from .bdaddr import normalize

    wanted = normalize(address)
    for device in enumerate_sinks():
        if device.address == wanted:
            return device
    return None
