"""Shared constants and small helpers of the Sonatina host (audio format, codec ids,
paths, user-interface language)."""

from __future__ import annotations

import ctypes
import logging
import os
import sys

log = logging.getLogger("sonatina")


def _ui_is_italian() -> bool:
    try:
        if sys.platform == "win32":
            return (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x10
        return os.environ.get("LANG", "").lower().startswith("it")
    except Exception:
        return False


UI_ITALIAN = _ui_is_italian()


def tr(italian: str, english: str) -> str:
    """User-facing text in the Windows display language (Italian or English)."""
    return italian if UI_ITALIAN else english

HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))  # PyInstaller-aware
RATE = 48000
CHANNELS = 2
FRAME_BYTES = 2 * CHANNELS            # int16 stereo
PUSH_SAMPLES = 128                    # LDAC and SBC (16 blocks x 8 subbands) both take 128
LDAC_VENDOR_ID = 0x0000012D           # Sony
LDAC_CODEC_ID = 0x00AA
LDAC_EQMID = {"hq": 0, "sq": 1, "mq": 2}
LDAC_KBPS = {0: 990, 1: 660, 2: 330}
LDAC_DELAY_MS = 15                    # estimate: libldac look-ahead + 2-3 frames per packet
NORMAL_TARGET_MS = 20                 # jitter buffer: safety margin kept at the worst moment
LOW_LATENCY_TARGET_MS = 8
LOW_LATENCY_PACKET_MS = 0             # packet cap in low latency (0 = fill the MTU). 8 ms saved
                                      # ~4 ms but doubled the packet rate: pops on the Air5's
                                      # second earbud (true-wireless relay), 2026-11-19
RTP_HEADER = 12
APTX_VENDOR_ID = 0x0000004F           # APT Ltd. (Qualcomm)
APTX_CODEC_ID = 0x0001
APTX_HD_VENDOR_ID = 0x000000D7        # Qualcomm Technologies International
APTX_HD_CODEC_ID = 0x0024
VENDOR_CODECS = {
    (LDAC_VENDOR_ID, LDAC_CODEC_ID): "LDAC",
    (APTX_VENDOR_ID, APTX_CODEC_ID): "aptX",
    (APTX_HD_VENDOR_ID, APTX_HD_CODEC_ID): "aptX HD",
    (0x000000D7, 0x00AD): "aptX Adaptive",
    (0x0000000A, 0x0002): "aptX Low Latency",
}
# Preferred codec first, then the next ones (same order as Android's codec priority)
CODEC_CHAIN = ["ldac", "aptx_hd", "aptx", "aac", "sbc"]
CODEC_NAMES = {"ldac": "LDAC", "aptx_hd": "aptX HD", "aptx": "aptX", "aac": "AAC", "sbc": "SBC"}
AAC_FRAME = 1024                      # samples per AAC access unit (one RTP packet)
SBC_MIN_ADAPTIVE_BITPOOL = 24         # adaptive quality never goes below this (about 170 kbit/s)
AAC_MIN_ADAPTIVE_BITRATE = 128000     # nor below this for AAC
AAC_LATM_OVERHEAD = 16                # StreamMuxConfig + PayloadLengthInfo, rounded up
SSRC = 0x534F4E54                     # "SONT"


def pushes_for(max_packet_ms: int) -> int:
    """How many 128-sample pushes fit in max_packet_ms (0 = no limit)."""
    if max_packet_ms <= 0:
        return 1 << 30
    return max(1, max_packet_ms * RATE // 1000 // PUSH_SAMPLES)


def com_init() -> None:
    """Initialises COM on the calling thread. pycaw needs it in every thread that
    touches the audio endpoints; comtypes only does it for the thread that first
    imports it, which in the frozen tray app is not the host thread."""
    if sys.platform != "win32":
        return
    import comtypes
    try:
        comtypes.CoInitialize()
    except OSError as error:    # RPC_E_CHANGED_MODE: already initialised as MTA, fine
        log.debug("CoInitialize: %r", error)


def norm_addr(address) -> str:
    """'2c:be:..:2e/P' -> '2C:BE:..:2E'"""
    return str(address).split("/")[0].upper()


APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Sonatina")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
IPC_PATH = os.path.join(APP_DIR, "ipc.json")


def app_version() -> str:
    """Written into version.txt by the CI for installed builds; "dev" otherwise."""
    try:
        with open(os.path.join(HERE, "version.txt"), encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"
