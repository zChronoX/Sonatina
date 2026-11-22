"""Sonatina settings (settings.json in %LOCALAPPDATA%\\Sonatina)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("sonatina")


@dataclass
class Settings:
    transport: str = "usb:0BDA:8771"
    address: str = ""             # preferred headset ("" = the first paired one)
    codec: str = "ldac"           # preferred codec: ldac | aptx_hd | aptx | aac | sbc (per headset
                                  # in `headsets`); unsupported -> the next one in CODEC_CHAIN
    aac_bitrate: int = 256000     # AAC target, lowered to fit the headset MTU and max bitrate
    ldac_quality: str = "sq"      # hq | sq | mq
    sbc_max_bitpool: int = 53
    capture: str = "CABLE Output"
    volume_endpoint: str = "VB-Audio Virtual Cable"
    tone: bool = False
    stats: bool = True
    allow_unknown: bool = False   # accept connections from headsets we never paired
    audio_configured: bool = False  # VB-CABLE already made the default output once
    absolute_volume: bool = True  # AVRCP: volume on the headset when it supports it
    low_latency: bool = False     # smaller jitter buffer and packets (games, video)
    media_keys: bool = True       # AVRCP: headset buttons -> Windows media keys
    ldac_adaptive: bool = True    # LDAC: lower the bitrate while the radio cannot keep up
    headsets: dict = field(default_factory=dict)  # "AA:BB:..": {"name", "codec", "ldac_quality"}
    hfp_mode: str = "auto"        # headset microphone: auto (when an app opens the virtual mic) |
                                  # on (call mode whenever connected) | off (no HFP, no SCO)
    hfp_msbc: bool = True         # wide-band speech (mSBC 16 kHz) when the headset has it
    mic_device: str = ""          # virtual cable for the microphone ("" = first found of
                                  # CABLE-A, Hi-Fi Cable, Steam Streaming Microphone)
    mic_gain_db: float = 6.0      # headset microphones are quiet
    sco_alt: int = 2              # USB alternate setting for CVSD (2 = one 16-bit link)
    sco_alt_msbc: int = 1         # USB alternate setting for mSBC (transparent, 24-byte packets)
    # automatic Windows output (sonatina_output)
    auto_output: bool = True      # headphones on: "Sonatina Audio"; off: the fallback below
    pause_on_disconnect: bool = True
    fallback_output: str = ""     # endpoint id chosen by the user ("" = automatic)
    fallback_output_name: str = ""
    last_output: str = ""         # the last real output the user had (automatic fallback)
    last_output_name: str = ""
    # radio (sonatina_radio)
    wifi_avoid: bool = True       # keep Bluetooth off the channels of nearby 2.4 GHz Wi-Fi
    wifi_channel: int = 0         # Wi-Fi channel to avoid chosen by hand (0 = from the PC's Wi-Fi)


SAVED_FIELDS = ("transport", "address", "codec", "ldac_quality", "sbc_max_bitpool", "aac_bitrate",
                "capture",
                "volume_endpoint", "headsets", "audio_configured", "absolute_volume", "media_keys", "low_latency",
                "ldac_adaptive",
                "hfp_mode", "hfp_msbc", "mic_device", "mic_gain_db", "sco_alt", "sco_alt_msbc",
                "auto_output", "pause_on_disconnect", "fallback_output", "fallback_output_name",
                "last_output", "last_output_name", "wifi_avoid", "wifi_channel")


def load_settings(path: str) -> Settings:
    s = Settings()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for key in SAVED_FIELDS:
            if key in data:
                setattr(s, key, data[key])
    except FileNotFoundError:
        pass
    except Exception as error:
        log.warning("settings %s unreadable (%r): using defaults", path, error)
    return s


def save_settings(s: Settings, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({key: getattr(s, key) for key in SAVED_FIELDS}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
