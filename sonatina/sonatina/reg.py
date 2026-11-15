"""Registry profile store.

Sonatina keeps one profile per device and a small live telemetry block. The
layout is:

    HKCU\\Software\\Sonatina\\Devices\\<BD_ADDR>\\   Codec, Sbc*, Aac*
                                               \\Capability\\  (written by the sink)
                                               \\Current\\     (written by the source)
    HKCU\\Software\\Sonatina\\Control\\Trigger       (UI -> service signal)
"""

from __future__ import annotations

import winreg
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import REG_ROOT
from .bdaddr import normalize
from .sbc import SbcParams

HIVES = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
MODE_DWORD = {"mono": 0, "dual": 1, "stereo": 2, "joint": 3}
MODE_BY_DWORD = {v: k for k, v in MODE_DWORD.items()}
ALLOC_DWORD = {"snr": 0, "loudness": 1}
ALLOC_BY_DWORD = {v: k for k, v in ALLOC_DWORD.items()}

CURRENT_SCHEMA = {
    "Codec": "sz",
    "Bitrate": "dword",
    "Packets": "dword",
    "Errors": "dword",
    "TxTimeUs": "dword",
    "DelayMs": "dword",
    "Opened": "dword",
    "UpdatedAt": "sz",
}


class RegistryError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Profile:
    device: str
    codec: str = "sbc"
    sbc: SbcParams = field(default_factory=SbcParams)
    aac_channel_mode: int = 1
    aac_sampling: int = 48000
    aac_bitrate: int = 256

    def summary(self) -> str:
        if self.codec == "sbc":
            s = self.sbc
            return (
                "codec=sbc sampling=%d mode=%s subbands=%d blocks=%d alloc=%s bitpool=%d..%d"
                % (
                    s.sampling,
                    s.channel_mode,
                    s.subbands,
                    s.blocks,
                    s.allocation,
                    s.min_bitpool,
                    s.max_bitpool,
                )
            )
        if self.codec == "aac":
            return "codec=aac sampling=%d channels=%d bitrate=%d" % (
                self.aac_sampling,
                2 if self.aac_channel_mode else 1,
                self.aac_bitrate,
            )
        return "codec=%s" % self.codec


def _set_dword(key, name: str, value: int) -> None:
    winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value) & 0xFFFFFFFF)


def _set_sz(key, name: str, value: str) -> None:
    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))


def _get(key, name: str, default=None):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return default
    except OSError:
        return default


def _open_path(hive, path: str, access: int = winreg.KEY_READ):
    return winreg.OpenKey(hive, path, 0, access)


def _create_path(hive, path: str):
    return winreg.CreateKeyEx(hive, path, 0, winreg.KEY_WRITE | winreg.KEY_READ)


class Store:
    def __init__(self, hive: str = "HKCU", root: str = REG_ROOT) -> None:
        if hive not in HIVES:
            raise ValueError("hive must be HKCU or HKLM, got %r" % hive)
        self.hive_name = hive
        self.hive = HIVES[hive]
        self.root = root

    # -- paths --------------------------------------------------------------

    @property
    def devices_path(self) -> str:
        return self.root + r"\Devices"

    @property
    def control_path(self) -> str:
        return self.root + r"\Control"

    @property
    def watch_path(self) -> str:
        return self.control_path

    def device_path(self, address: str) -> str:
        return self.devices_path + "\\" + normalize(address)

    # -- lifecycle ----------------------------------------------------------

    def ensure(self) -> None:
        try:
            key = _create_path(self.hive, self.devices_path)
            winreg.CloseKey(key)
            key = _create_path(self.hive, self.control_path)
            if _get(key, "Trigger") is None:
                _set_dword(key, "Trigger", 0)
            winreg.CloseKey(key)
        except OSError as exc:
            raise RegistryError(
                "cannot create %s\\%s: %s" % (self.hive_name, self.root, exc)
            )

    def exists(self) -> bool:
        try:
            winreg.CloseKey(_open_path(self.hive, self.root))
            return True
        except OSError:
            return False

    # -- profiles -----------------------------------------------------------

    def list_addresses(self) -> list[str]:
        addresses = []
        try:
            key = _open_path(self.hive, self.devices_path)
        except OSError:
            return addresses
        try:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                try:
                    addresses.append(normalize(name))
                except ValueError:
                    continue
        finally:
            winreg.CloseKey(key)
        return addresses

    def read_profile(self, address: str) -> Profile | None:
        path = self.device_path(address)
        try:
            key = _open_path(self.hive, path)
        except OSError:
            return None
        try:
            defaults = SbcParams()
            codec = _get(key, "Codec", "sbc")
            sbc = SbcParams(
                sampling=int(_get(key, "SbcSamplingFrequency", defaults.sampling)),
                channel_mode=MODE_BY_DWORD.get(
                    int(_get(key, "SbcChannelMode", 3)), "joint"
                ),
                blocks=int(_get(key, "SbcBlockLength", defaults.blocks)),
                subbands=int(_get(key, "SbcSubbands", defaults.subbands)),
                allocation=ALLOC_BY_DWORD.get(
                    int(_get(key, "SbcAllocationMethod", 1)), "loudness"
                ),
                min_bitpool=int(_get(key, "SbcMinimumBitpool", defaults.min_bitpool)),
                max_bitpool=int(_get(key, "SbcMaximumBitpool", defaults.max_bitpool)),
            )
            if codec not in ("pcm", "sbc", "aac", "aptx"):
                codec = "sbc"
            return Profile(
                device=normalize(address),
                codec=codec,
                sbc=sbc,
                aac_channel_mode=int(_get(key, "AacChannelMode", 1)),
                aac_sampling=int(_get(key, "AacSamplingFrequency", 48000)),
                aac_bitrate=int(_get(key, "AacBitrate", 256)),
            )
        finally:
            winreg.CloseKey(key)

    def write_profile(self, profile: Profile) -> None:
        profile.sbc.validate()
        path = self.device_path(profile.device)
        try:
            key = _create_path(self.hive, path)
        except OSError as exc:
            raise RegistryError("cannot create %s: %s" % (path, exc))
        try:
            sbc = profile.sbc
            _set_sz(key, "Codec", profile.codec)
            _set_dword(key, "SbcChannelMode", MODE_DWORD[sbc.channel_mode])
            _set_dword(key, "SbcSamplingFrequency", sbc.sampling)
            _set_dword(key, "SbcSubbands", sbc.subbands)
            _set_dword(key, "SbcBlockLength", sbc.blocks)
            _set_dword(key, "SbcAllocationMethod", ALLOC_DWORD[sbc.allocation])
            _set_dword(key, "SbcMinimumBitpool", sbc.min_bitpool)
            _set_dword(key, "SbcMaximumBitpool", sbc.max_bitpool)
            _set_dword(key, "AacChannelMode", profile.aac_channel_mode)
            _set_dword(key, "AacSamplingFrequency", profile.aac_sampling)
            _set_dword(key, "AacBitrate", profile.aac_bitrate)
            _set_sz(key, "UpdatedAt", now_iso())
        finally:
            winreg.CloseKey(key)

    def delete_profile(self, address: str) -> bool:
        path = self.device_path(address)
        try:
            winreg.DeleteKey(self.hive, path + r"\Capability")
        except OSError:
            pass
        try:
            winreg.DeleteKey(self.hive, path + r"\Current")
        except OSError:
            pass
        try:
            winreg.DeleteKey(self.hive, path)
            return True
        except OSError:
            return False

    # -- capability (written by the sink after GetCapabilities) --------------

    def write_capability(
        self,
        address: str,
        codecs: list[str],
        sbc: SbcParams | None = None,
        aac_bitrate: int = 0,
    ) -> None:
        path = self.device_path(address) + r"\Capability"
        key = _create_path(self.hive, path)
        try:
            for name in ("sbc", "aac", "aptx", "pcm"):
                _set_dword(key, "Codec" + name.capitalize(), 1 if name in codecs else 0)
            _set_dword(key, "SbcMaxBitpool", sbc.max_bitpool if sbc else 0)
            _set_dword(key, "AacMaxBitrate", aac_bitrate)
            _set_sz(key, "UpdatedAt", now_iso())
        finally:
            winreg.CloseKey(key)

    def read_capability(self, address: str) -> dict | None:
        path = self.device_path(address) + r"\Capability"
        try:
            key = _open_path(self.hive, path)
        except OSError:
            return None
        try:
            return {
                "Sbc": bool(_get(key, "CodecSbc", 0)),
                "Aac": bool(_get(key, "CodecAac", 0)),
                "Aptx": bool(_get(key, "CodecAptx", 0)),
                "Pcm": bool(_get(key, "CodecPcm", 0)),
                "SbcMaxBitpool": int(_get(key, "SbcMaxBitpool", 0)),
                "AacMaxBitrate": int(_get(key, "AacMaxBitrate", 0)),
                "UpdatedAt": _get(key, "UpdatedAt", ""),
            }
        finally:
            winreg.CloseKey(key)

    # -- current telemetry (written by the source at 1 Hz) -------------------

    def write_current(self, address: str, **stats) -> None:
        path = self.device_path(address) + r"\Current"
        key = _create_path(self.hive, path)
        try:
            for name, kind in CURRENT_SCHEMA.items():
                if name not in stats:
                    continue
                if kind == "dword":
                    _set_dword(key, name, int(stats[name]))
                else:
                    _set_sz(key, name, str(stats[name]))
        finally:
            winreg.CloseKey(key)

    def read_current(self, address: str) -> dict | None:
        path = self.device_path(address) + r"\Current"
        try:
            key = _open_path(self.hive, path)
        except OSError:
            return None
        try:
            out = {}
            for name, kind in CURRENT_SCHEMA.items():
                value = _get(key, name)
                if value is None:
                    continue
                out[name] = int(value) if kind == "dword" else str(value)
            return out
        finally:
            winreg.CloseKey(key)

    # -- Trigger ------------------------------------------------------------

    def read_trigger(self) -> int:
        try:
            key = _open_path(self.hive, self.control_path)
        except OSError:
            return 0
        try:
            return int(_get(key, "Trigger", 0))
        finally:
            winreg.CloseKey(key)

    def bump_trigger(self) -> int:
        self.ensure()
        key = _open_path(
            self.hive, self.control_path, winreg.KEY_READ | winreg.KEY_WRITE
        )
        try:
            value = (int(_get(key, "Trigger", 0)) + 1) & 0xFFFFFFFF
            _set_dword(key, "Trigger", value)
            return value
        finally:
            winreg.CloseKey(key)
