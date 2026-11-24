"""Constants shared by the HFP modules (split from sonatina_hfp in 1.3.0)."""

from __future__ import annotations

RATE = 48000
MIC_DEVICES = ("CABLE-A", "Hi-Fi Cable", "Steam Streaming Microphone")
AG_RECORD_HANDLE = 0x00010004
H2_SYNC = (0x08, 0x38, 0xC8, 0xF8)
MSBC_FRAME = 60          # H2 header (2) + mSBC (57) + padding (1)
MSBC_SAMPLES = 120       # 7.5 ms at 16 kHz
VOICE_CVSD = 0x0060      # linear PCM 16 bit 2's complement in, CVSD on air
VOICE_TRANSPARENT = 0x0063
# (name, max latency ms, retransmission effort, packet types) - HFP 1.8 tables 5.7/5.8
CVSD_SETTINGS = [("eSCO S4", 0x000C, 0x02, 0x0388), ("eSCO S1", 0x0007, 0x01, 0x0388),
                 ("SCO D1", 0xFFFF, 0x00, 0x03C4)]
MSBC_SETTINGS = [("eSCO T2", 0x000D, 0x02, 0x0388), ("eSCO T1", 0x0008, 0x02, 0x03C8)]


def tr(italian: str, english: str) -> str:
    import sonatina_host
    return sonatina_host.tr(italian, english)
