"""SonatinaHost (phase U1): Windows audio -> LDAC/SBC -> A2DP over a dedicated USB dongle.

Windows apps play to VB-CABLE ("CABLE Input"); this program captures "CABLE Output",
encodes with the Sonatina codecs (sonatina_codec.dll, same encoder code as the kernel
driver) and streams to the headset through a user-mode Bluetooth host (Bumble) that
drives a USB dongle bound to WinUSB. No kernel component of ours: Secure Boot stays on.

Usage (from this folder):
    python sonatina_host.py                      # LDAC to the configured headset
    python sonatina_host.py --codec sbc
    python sonatina_host.py --tone               # 1 kHz test tone instead of VB-CABLE
    python sonatina_host.py --list-devices       # audio capture devices
"""

from __future__ import annotations

import argparse
import array
import asyncio
import ctypes
import json
import logging
import math
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass, field

from bumble.a2dp import (
    A2DP_MPEG_2_4_AAC_CODEC_TYPE,
    A2DP_NON_A2DP_CODEC_TYPE,
    A2DP_SBC_CODEC_TYPE,
    AacMediaCodecInformation,
    SbcMediaCodecInformation,
    VendorSpecificMediaCodecInformation,
    make_audio_source_service_sdp_records,
)
from bumble.avdtp import (
    AVDTP_AUDIO_MEDIA_TYPE,
    AVDTP_DELAY_REPORTING_SERVICE_CATEGORY,
    MediaCodecCapabilities,
    find_avdtp_service_with_connection,
)
from bumble.avdtp import Protocol as AvdtpProtocol
from bumble.core import AdvertisingData, DeviceClass, PhysicalTransport
from bumble.device import Connection, Device, DeviceConfiguration
from bumble.pairing import PairingConfig

import sonatina_avrcp
import sonatina_media

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
AAC_LATM_OVERHEAD = 16                # StreamMuxConfig + PayloadLengthInfo, rounded up
SSRC = 0x534F4E54                     # "SONT"


def pushes_for(max_packet_ms: int) -> int:
    """How many 128-sample pushes fit in max_packet_ms (0 = no limit)."""
    if max_packet_ms <= 0:
        return 1 << 30
    return max(1, max_packet_ms * RATE // 1000 // PUSH_SAMPLES)


# ----------------------------------------------------------------------------- codec dll
class CodecLib:
    def __init__(self) -> None:
        name = "sonatina_codec.dll" if sys.platform == "win32" else "sonatina_codec.so"
        path = os.path.join(HERE, "codec", name)
        self.lib = ctypes.CDLL(path)
        L = self.lib
        L.sont_codec_version.restype = ctypes.c_int
        L.sont_ldac_create.restype = ctypes.c_void_p
        L.sont_ldac_create.argtypes = [ctypes.c_uint] * 4
        L.sont_ldac_destroy.argtypes = [ctypes.c_void_p]
        L.sont_ldac_set_eqmid.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        L.sont_ldac_encode.restype = ctypes.c_int
        L.sont_ldac_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                                       ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
        L.sont_sbc_create.restype = ctypes.c_void_p
        L.sont_sbc_create.argtypes = [ctypes.c_uint] * 6
        L.sont_sbc_destroy.argtypes = [ctypes.c_void_p]
        L.sont_sbc_frame_bytes.restype = ctypes.c_uint
        L.sont_sbc_frame_bytes.argtypes = [ctypes.c_uint] * 6
        L.sont_sbc_encode.restype = ctypes.c_int
        L.sont_sbc_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint,
                                      ctypes.c_char_p, ctypes.c_uint]
        self.has_aac = hasattr(L, "sont_aac_create")  # codec version >= 3
        if self.has_aac:
            L.sont_aac_create.restype = ctypes.c_void_p
            L.sont_aac_create.argtypes = [ctypes.c_uint] * 5
            L.sont_aac_destroy.argtypes = [ctypes.c_void_p]
            L.sont_aac_get.restype = ctypes.c_int
            L.sont_aac_get.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            L.sont_aac_encode.restype = ctypes.c_int
            L.sont_aac_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint,
                                          ctypes.c_uint, ctypes.c_char_p, ctypes.c_uint]
        self.has_aptx = hasattr(L, "sont_aptx_create")  # codec version >= 4
        if self.has_aptx:
            L.sont_aptx_create.restype = ctypes.c_void_p
            L.sont_aptx_create.argtypes = [ctypes.c_uint]
            L.sont_aptx_destroy.argtypes = [ctypes.c_void_p]
            L.sont_aptx_encode.restype = ctypes.c_int
            L.sont_aptx_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint,
                                           ctypes.c_char_p, ctypes.c_uint]
        self.has_gain = hasattr(L, "sont_pcm_gain")   # codec version >= 2
        if self.has_gain:
            L.sont_pcm_gain.restype = None
            L.sont_pcm_gain.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint,
                                        ctypes.c_int, ctypes.c_int]


class LdacEncoder:
    """128 samples in, an RTP payload (frame-count byte + LDAC frames) out every few pushes."""

    name = "LDAC"

    def __init__(self, lib: CodecLib, eqmid: int, mtu: int) -> None:
        self.lib = lib.lib
        self.handle = self.lib.sont_ldac_create(RATE, 2, eqmid, 679)
        if not self.handle:
            raise RuntimeError("LDAC encoder init failed")
        self.out = ctypes.create_string_buffer(1024)
        self.written = ctypes.c_uint()
        self.eqmid = eqmid
        self.max_payload = mtu - RTP_HEADER
        self.kbps = LDAC_KBPS[eqmid]
        self.delay_ms = LDAC_DELAY_MS

    def push(self, pcm: bytes) -> list[tuple[bytes, int]]:
        frames = self.lib.sont_ldac_encode(self.handle, pcm, self.out, 1024, ctypes.byref(self.written))
        if frames < 0:
            raise RuntimeError("LDAC encode failed")
        if frames == 0:
            return []
        payload = bytes([frames & 0x0F]) + self.out.raw[: self.written.value]
        if len(payload) > self.max_payload:
            log.warning("LDAC packet %d bytes > MTU payload %d", len(payload), self.max_payload)
        return [(payload, frames * 128)]

    def close(self) -> None:
        if self.handle:
            self.lib.sont_ldac_destroy(self.handle)
            self.handle = None


class SbcEncoder:
    """48 kHz, joint stereo, 16 blocks, 8 subbands, loudness; packs frames up to the MTU."""

    name = "SBC"

    def __init__(self, lib: CodecLib, bitpool: int, mtu: int, max_packet_ms: int = 0) -> None:
        self.lib = lib.lib
        self.bitpool = bitpool
        self.handle = self.lib.sont_sbc_create(RATE, 3, 16, 8, 1, bitpool)
        if not self.handle:
            raise RuntimeError("SBC encoder init failed")
        self.frame_bytes = self.lib.sont_sbc_frame_bytes(RATE, 3, 16, 8, 1, bitpool)
        self.per_packet = max(1, min(15, (mtu - RTP_HEADER - 1) // self.frame_bytes,
                                     pushes_for(max_packet_ms)))
        self.delay_ms = self.per_packet * PUSH_SAMPLES * 1000 // RATE
        self.out = ctypes.create_string_buffer(512)
        self.pending: list[bytes] = []
        self.kbps = self.frame_bytes * 8 * RATE // 128 // 1000

    def push(self, pcm: bytes) -> list[tuple[bytes, int]]:
        n = self.lib.sont_sbc_encode(self.handle, pcm, 128, self.out, 512)
        if n <= 0:
            raise RuntimeError("SBC encode failed")
        self.pending.append(self.out.raw[:n])
        if len(self.pending) < self.per_packet:
            return []
        payload = bytes([len(self.pending)]) + b"".join(self.pending)
        samples = len(self.pending) * 128
        self.pending = []
        return [(payload, samples)]

    def close(self) -> None:
        if self.handle:
            self.lib.sont_sbc_destroy(self.handle)
            self.handle = None


class AacEncoder:
    """AAC-LC (FDK), 48 kHz stereo, LATM: 1024 samples in -> one RTP payload out.

    The peak bitrate is bounded by the media MTU, so no access unit ever exceeds a packet
    (the same rule as Android's A2DP AAC encoder); the average stays a bit below it so
    the bit reservoir can work."""

    name = "AAC"

    def __init__(self, lib: CodecLib, bitrate: int, mtu: int) -> None:
        if not lib.has_aac:
            raise RuntimeError("sonatina_codec.dll is too old for AAC: update it")
        self.lib = lib.lib
        self.max_payload = mtu - RTP_HEADER
        peak = (self.max_payload - AAC_LATM_OVERHEAD) * 8 * RATE // AAC_FRAME
        target = min(bitrate, peak * 9 // 10)
        self.handle = self.lib.sont_aac_create(RATE, CHANNELS, target, peak, 0)
        if not self.handle:
            raise RuntimeError("AAC encoder init failed")
        self.bitrate = self.lib.sont_aac_get(self.handle, 0)
        self.peak = self.lib.sont_aac_get(self.handle, 1)
        self.kbps = self.bitrate // 1000
        self.delay_ms = (self.lib.sont_aac_get(self.handle, 2) + AAC_FRAME) * 1000 // RATE
        self.pcm = bytearray()
        self.out = ctypes.create_string_buffer(8192)
        self.oversize = 0
        log.info("AAC: %d kbit/s, peak %d kbit/s (MTU %d), delay %d samples", self.kbps,
                 self.peak // 1000, mtu, self.lib.sont_aac_get(self.handle, 2))

    def push(self, pcm: bytes) -> list[tuple[bytes, int]]:
        self.pcm += pcm
        block = AAC_FRAME * FRAME_BYTES
        packets = []
        while len(self.pcm) >= block:
            n = self.lib.sont_aac_encode(self.handle, bytes(self.pcm[:block]), AAC_FRAME,
                                         CHANNELS, self.out, len(self.out))
            del self.pcm[:block]
            if n < 0:
                raise RuntimeError("AAC encode failed")
            if n == 0:
                continue   # encoder look-ahead
            if n > self.max_payload:   # must not happen with the peak bitrate set
                self.oversize += 1
                if self.oversize == 1:
                    log.warning("AAC frame of %d bytes > %d: dropped", n, self.max_payload)
                continue
            packets.append((self.out.raw[:n], AAC_FRAME))
        return packets

    def close(self) -> None:
        if self.handle:
            self.lib.sont_aac_destroy(self.handle)
            self.handle = None


class AptxEncoder:
    """aptX (4:1 on 16-bit, 384 kbit/s at 48 kHz) or aptX HD (24-bit, 576 kbit/s).

    A2DP framing as in Android and PipeWire: aptX packets carry no RTP header, aptX HD
    packets do. Pushes of 128 samples are packed up to the MTU."""

    def __init__(self, lib: CodecLib, hd: bool, mtu: int, max_packet_ms: int = 0) -> None:
        if not lib.has_aptx:
            raise RuntimeError("sonatina_codec.dll is too old for aptX: update it")
        self.lib = lib.lib
        self.name = "aptX HD" if hd else "aptX"
        self.rtp = hd
        self.handle = self.lib.sont_aptx_create(1 if hd else 0)
        if not self.handle:
            raise RuntimeError("aptX encoder init failed")
        self.push_bytes = PUSH_SAMPLES * (6 if hd else 4) // 4
        room = mtu - (RTP_HEADER if hd else 0)
        self.per_packet = max(1, min(room // self.push_bytes, pushes_for(max_packet_ms)))
        self.delay_ms = self.per_packet * PUSH_SAMPLES * 1000 // RATE + 2   # + 90-sample codec delay
        self.kbps = self.push_bytes * 8 * RATE // PUSH_SAMPLES // 1000
        self.out = ctypes.create_string_buffer(1024)
        self.pending: list[bytes] = []

    def push(self, pcm: bytes) -> list[tuple[bytes, int]]:
        n = self.lib.sont_aptx_encode(self.handle, pcm, PUSH_SAMPLES, self.out, len(self.out))
        if n != self.push_bytes:
            raise RuntimeError(f"aptX encode failed ({n})")
        self.pending.append(self.out.raw[:n])
        if len(self.pending) < self.per_packet:
            return []
        payload = b"".join(self.pending)
        samples = len(self.pending) * PUSH_SAMPLES
        self.pending = []
        return [(payload, samples)]

    def close(self) -> None:
        if self.handle:
            self.lib.sont_aptx_destroy(self.handle)
            self.handle = None


# ----------------------------------------------------------------------------- PCM ring
class PcmRing:
    """Thread-safe jitter buffer between the capture thread and the media pump."""

    def __init__(self, prefill_ms: int = 40, max_ms: int = 200) -> None:
        self.lock = threading.Lock()
        self.buf = bytearray()
        self.prefill = RATE * prefill_ms // 1000 * FRAME_BYTES
        self.max = RATE * max_ms // 1000 * FRAME_BYTES
        self.playing = False
        self.underruns = 0
        self.dropped = 0
        self.min_level = None          # lowest fill after a read since take_min(), bytes

    def set_prefill(self, ms: int) -> None:
        with self.lock:
            self.prefill = RATE * ms // 1000 * FRAME_BYTES

    def take_min_ms(self) -> float | None:
        """Lowest fill seen by the reader since the last call (the safety margin left)."""
        with self.lock:
            low, self.min_level = self.min_level, None
        return None if low is None else low * 1000 / (RATE * FRAME_BYTES)

    def write(self, data: bytes) -> None:
        with self.lock:
            self.buf += data
            if len(self.buf) > self.max:
                drop = len(self.buf) - self.prefill
                drop -= drop % FRAME_BYTES
                del self.buf[:drop]
                self.dropped += drop

    def read(self, nbytes: int) -> bytes:
        with self.lock:
            if not self.playing:
                if len(self.buf) < self.prefill:
                    return bytes(nbytes)
                self.playing = True
            if len(self.buf) < nbytes:
                self.playing = False
                self.underruns += 1
                return bytes(nbytes)
            out = bytes(self.buf[:nbytes])
            del self.buf[:nbytes]
            if self.min_level is None or len(self.buf) < self.min_level:
                self.min_level = len(self.buf)
            return out

    def reset(self) -> None:
        """Drops what was captured while nothing was streaming (stale audio)."""
        with self.lock:
            self.buf.clear()
            self.playing = False
            self.dropped = 0

    def level_ms(self) -> int:
        with self.lock:
            return len(self.buf) * 1000 // (RATE * FRAME_BYTES)


# ----------------------------------------------------------------------------- capture
class ToneSource:
    def __init__(self, ring: PcmRing, freq: float = 1000.0, speed: float = 1.0) -> None:
        self.ring = ring
        self.freq = freq
        self.speed = speed          # != 1: simulates a capture clock that drifts (tests)
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        n = 0
        t0 = time.perf_counter()
        while True:
            due = int((time.perf_counter() - t0) * RATE * self.speed)
            chunk = []
            while n < due:
                v = int(6000 * math.sin(2 * math.pi * self.freq * n / RATE))
                chunk.append(struct.pack("<hh", v, v))
                n += 1
            if chunk:
                self.ring.write(b"".join(chunk))
            await asyncio.sleep(0.005)

    def start(self) -> None:
        self.task = asyncio.get_running_loop().create_task(self.run())

    def stop(self) -> None:
        if self.task:
            self.task.cancel()

    def check(self) -> None:
        pass


def find_capture_device(name: str):
    import sounddevice as sd

    best = None
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] < 1 or name.lower() not in dev["name"].lower():
            continue
        api = sd.query_hostapis(dev["hostapi"])["name"]
        if "WASAPI" in api:
            return index, dev, api
        if best is None:
            best = (index, dev, api)
    if best is None:
        raise RuntimeError(f'no capture device matching "{name}" (is VB-CABLE installed?)')
    return best


class CableCapture:
    """Captures the VB-CABLE output (what Windows plays to "CABLE Input")."""

    def _open(self) -> None:
        import sounddevice as sd

        self.sd = sd
        index, dev, api = find_capture_device(self.device_name)
        extra = None
        if "WASAPI" in api:
            try:
                extra = sd.WasapiSettings(auto_convert=True)
            except TypeError:
                extra = sd.WasapiSettings()
        log.info("capture: [%d] %s (%s)", index, dev["name"], api)
        self.stream = sd.RawInputStream(
            device=index, samplerate=RATE, channels=CHANNELS, dtype="int16",
            blocksize=RATE // 100, latency="low", callback=self._callback,
            extra_settings=extra,
        )

    def __init__(self, ring: PcmRing, device_name: str) -> None:
        self.ring = ring
        self.device_name = device_name
        self.stream = None
        self.running = False
        self.last_callback = 0.0
        self.retry_at = 0.0
        self.restarts = 0
        self._open()

    def _callback(self, indata, frames, time_info, status) -> None:
        self.last_callback = time.monotonic()
        if status:
            log.debug("capture status: %s", status)
        self.ring.write(bytes(indata))

    def start(self) -> None:
        self.running = True
        self.last_callback = time.monotonic()
        self.stream.start()

    def stop(self) -> None:
        self.running = False
        self._close()

    def _close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.abort()
                self.stream.close()
            except Exception as error:
                log.debug("capture close: %r", error)
            self.stream = None

    def check(self) -> None:
        """Watchdog: WASAPI streams die when the device goes away (sleep, VB-CABLE
        reinstalled, audio service restarted). Reopen after 3 s without data."""
        now = time.monotonic()
        if not self.running or now - self.last_callback < 3.0 or now < self.retry_at:
            return
        self.retry_at = now + 5.0
        log.warning("no audio from %s for %.0f s: reopening the capture", self.device_name,
                    now - self.last_callback)
        self._close()
        try:
            try:   # PortAudio caches the device list: refresh it (the cable may be new)
                self.sd._terminate()
                self.sd._initialize()
            except Exception as error:
                log.debug("PortAudio refresh: %r", error)
            self._open()
            self.start()
            self.restarts += 1
        except Exception as error:
            log.warning("capture reopen failed (%r), retrying", error)


# ----------------------------------------------------------------------------- volume
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


class EndpointVolume:
    """Follows the Windows volume and mute of the VB-CABLE playback endpoint.

    VB-CABLE ignores its own volume slider (the signal on "CABLE Output" is always
    full scale), so the gain is applied here, before encoding. When the headset has
    AVRCP absolute volume, `hardware` is set: the headset does the attenuation and the
    gain here only mutes."""

    UNITY = 65536

    def __init__(self, lib: CodecLib, match: str) -> None:
        self.lib = lib.lib
        self.gain = self.UNITY      # applied at the end of the last block (Q16)
        self.target = self.UNITY    # from Windows
        self.endpoint = None
        self.name = None
        self.last = None
        self.scalar: float | None = None   # Windows slider 0..1
        self.hardware = False               # volume done by the headset (AVRCP)
        self.match = match
        self.retry_at = 0.0                 # next attempt to find the endpoint
        self.warned = False
        if sys.platform != "win32":
            return
        if not lib.has_gain:
            log.warning("sonatina_codec.dll is too old for volume control: update it")
            return
        self.reopen()

    def reopen(self) -> None:
        self.retry_at = time.monotonic() + 5.0
        try:
            self._open(self.match)
        except Exception as error:
            if not self.warned:   # retried every 5 s, logged once
                self.warned = True
                log.warning("Windows volume not followed yet (%r)", error)

    def _open(self, match: str) -> None:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL

        com_init()
        from pycaw.constants import DEVICE_STATE, EDataFlow
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        enumerator = AudioUtilities.GetDeviceEnumerator()
        try:
            default_id = enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender.value, 1).GetId()
        except Exception:
            default_id = None
        endpoints = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
        candidates, names = [], []
        for i in range(endpoints.GetCount()):
            dev = endpoints.Item(i)
            name = AudioUtilities.CreateDevice(dev).FriendlyName or ""
            names.append(name)
            if match.lower() in name.lower():
                # the Windows default output first, then "CABLE Input", then "CABLE In 16ch"
                rank = (0 if dev.GetId() == default_id else 1, 1 if "16ch" in name.lower() else 0)
                candidates.append((rank, i, name, dev))
        if not candidates:
            raise RuntimeError(f'no active playback endpoint matching "{match}" among {names}')
        _rank, _i, name, dev = min(candidates, key=lambda c: (c[0], c[1]))
        interface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        self.endpoint = cast(interface, POINTER(IAudioEndpointVolume))
        self.name = name
        log.info("volume: following \"%s\"", name)
        self.poll()

    def _read(self) -> tuple[bool, float, float] | None:
        """(mute, slider 0..1, level in dB) of the endpoint, None if not available."""
        if self.endpoint is None:
            return None
        return (bool(self.endpoint.GetMute()),
                float(self.endpoint.GetMasterVolumeLevelScalar()),
                float(self.endpoint.GetMasterVolumeLevel()))

    def _write(self, scalar: float, unmute: bool) -> None:
        if self.endpoint is None:
            return
        self.endpoint.SetMasterVolumeLevelScalar(scalar, None)
        if unmute:
            self.endpoint.SetMute(False, None)

    def set_scalar(self, scalar: float, unmute: bool = False) -> None:
        """Moves the Windows slider (volume changed from the headset)."""
        try:
            self._write(max(0.0, min(1.0, scalar)), unmute)
        except Exception as error:
            log.debug("volume set: %r", error)
        self.poll()

    def poll(self) -> None:
        if (self.endpoint is None and sys.platform == "win32" and self.retry_at
                and time.monotonic() >= self.retry_at):
            self.reopen()   # VB-CABLE installed/enabled later
            if self.endpoint is None:
                return
        try:
            state = self._read()
        except Exception as error:
            log.debug("volume poll: %r", error)
            return
        if state is None:
            return
        mute, scalar, db = state
        self.scalar = scalar
        if mute or scalar <= 0.0:
            self.target = 0
        elif self.hardware:
            self.target = self.UNITY
        else:
            self.target = min(self.UNITY, int(self.UNITY * 10 ** (db / 20)))
        state = (mute, round(scalar * 100))
        if state != self.last:
            self.last = state
            log.info("volume %d%%%s", state[1], " (muted)" if mute else "")

    def apply(self, pcm: bytes, frames: int) -> bytes:
        g0, g1 = self.gain, self.target
        if g0 == self.UNITY and g1 == self.UNITY:
            return pcm
        buf = ctypes.create_string_buffer(pcm, len(pcm))
        self.lib.sont_pcm_gain(buf, frames, CHANNELS, g0, g1)
        self.gain = g1
        return buf.raw


def set_default_output(match: str = "VB-Audio Virtual Cable") -> str:
    """Makes the VB-CABLE playback endpoint the Windows default output (all roles)."""
    from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
    from pycaw.pycaw import AudioUtilities

    com_init()
    enumerator = AudioUtilities.GetDeviceEnumerator()
    endpoints = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
    for i in range(endpoints.GetCount()):
        dev = endpoints.Item(i)
        name = AudioUtilities.CreateDevice(dev).FriendlyName or ""
        if match.lower() in name.lower() and "16ch" not in name.lower():
            AudioUtilities.SetDefaultDevice(
                dev.GetId(), roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
            return name
    raise RuntimeError(f'no playback endpoint matching "{match}" (is VB-CABLE installed?)')


# ----------------------------------------------------------------------------- media pump
SILENCE = 32   # about -60 dBFS: paused players send zeros, quiet music stays above this


def not_silent(pcm: bytes) -> bool:
    samples = array.array("h")
    samples.frombytes(pcm)
    return bool(samples) and (max(samples) > SILENCE or min(samples) < -SILENCE)


class MediaPump:
    """Paces encoding on the monotonic clock (like the kernel driver's media clock),
    builds RTP packets with timestamps in samples and writes them to the channel.
    Duck-typed for bumble.avdtp.LocalSource (start/stop).

    Clock servo: the capture side (VB-CABLE, Windows audio clock) and our clock never run
    at exactly the same rate, so the jitter buffer would slowly fill up (more latency) or
    run dry (gaps). Once a second the lowest buffer level is compared with the target and
    our media clock is sped up or slowed down by a few hundred ppm at most, inaudible and
    well inside what headsets accept. The target is the safety margin left at the worst
    moment; after an underrun it grows by 5 ms (Windows was late), up to 60 ms."""

    LEAD_S = 0.020
    MAX_BACKLOG_S = 0.200
    GAIN = 0.0001          # rate change per ms of level error (10 ms -> 0.1 %)
    GAIN_I = 0.000003      # per ms of error, per second (critically damped with GAIN)
    MAX_RATE = 0.002       # +-0.2 %
    MAX_TARGET_MS = 60

    def __init__(self, ring: PcmRing, make_encoder, volume: EndpointVolume | None = None,
                 target_ms: int = 20) -> None:
        self.ring = ring
        self.volume = volume
        self.make_encoder = make_encoder
        self.encoder = None
        self.task: asyncio.Task | None = None
        self.seq = 0
        self.timestamp = 0
        self.packets = 0
        self.skips = 0
        self.max_late_ms = 0.0
        self.last_audio = 0.0     # monotonic time of the last non-silent block (AVRCP status)
        self.target_ms = target_ms
        self.rate = 1.0           # media clock speed vs. perf_counter
        self.drift = 0.0          # learned clock offset (integral term)
        self.margin_ms: float | None = None
        self.capture_ms = 10      # capture block (CableCapture)

    async def start(self, rtp_channel) -> None:
        await self.stop()
        self.ring.set_prefill(self.target_ms + self.capture_ms)
        self.ring.reset()
        self.encoder = self.make_encoder()
        self.task = asyncio.get_running_loop().create_task(self._run(rtp_channel))
        log.info("streaming %s %d kbit/s", self.encoder.name, self.encoder.kbps)

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None
        if self.encoder:
            self.encoder.close()
            self.encoder = None

    def latency_ms(self) -> int:
        """Our part of the delay: capture block + jitter buffer + codec/packet."""
        level = self.ring.level_ms()
        return self.capture_ms + level + (self.encoder.delay_ms if self.encoder else 0)

    def _servo(self) -> None:
        low = self.ring.take_min_ms()
        if low is None:
            return
        self.margin_ms = low
        error = low - self.target_ms
        # PI: the proportional part reacts, the integral learns the steady clock offset
        self.drift = max(-self.MAX_RATE, min(self.MAX_RATE, self.drift + error * self.GAIN_I))
        self.rate = 1.0 + max(-self.MAX_RATE, min(self.MAX_RATE, self.drift + error * self.GAIN))

    def _on_underrun(self) -> None:
        if self.target_ms < self.MAX_TARGET_MS:
            self.target_ms = min(self.MAX_TARGET_MS, self.target_ms + 5)
            self.ring.set_prefill(self.target_ms + self.capture_ms)
            log.info("audio arrived late: jitter buffer raised to %d ms", self.target_ms)

    async def _run(self, channel) -> None:
        pcm_bytes = PUSH_SAMPLES * FRAME_BYTES
        rtp = getattr(self.encoder, "rtp", True)   # aptX (classic) packets have no RTP header
        last = time.perf_counter()
        media = 0.0                 # media time in seconds (runs at self.rate)
        next_servo = last + 1.0
        underruns = self.ring.underruns
        done = 0
        while True:
            now = time.perf_counter()
            media += (now - last) * self.rate
            last = now
            if now >= next_servo:
                next_servo = now + 1.0
                self._servo()
            target = int((media + self.LEAD_S) * RATE)
            backlog = target - done
            if backlog > self.MAX_BACKLOG_S * RATE:
                skip = backlog - int(self.LEAD_S * RATE)
                done += skip - skip % PUSH_SAMPLES
                self.skips += 1
            late_ms = (backlog / RATE - self.LEAD_S) * 1000
            if late_ms > self.max_late_ms:
                self.max_late_ms = late_ms
            while target - done >= PUSH_SAMPLES:
                pcm = self.ring.read(pcm_bytes)
                if self.ring.underruns != underruns:
                    underruns = self.ring.underruns
                    self._on_underrun()
                if not_silent(pcm):
                    self.last_audio = now
                if self.volume is not None:
                    pcm = self.volume.apply(pcm, PUSH_SAMPLES)
                for payload, samples in self.encoder.push(pcm):
                    header = struct.pack(">BBHII", 0x80, 96, self.seq & 0xFFFF,
                                         self.timestamp & 0xFFFFFFFF, SSRC) if rtp else b""
                    try:
                        channel.write(header + payload)
                    except Exception as error:   # link dropped under us: stop quietly
                        log.debug("media channel closed: %r", error)
                        return
                    self.seq += 1
                    self.timestamp += samples
                    self.packets += 1
                done += PUSH_SAMPLES
            next_media = (done + PUSH_SAMPLES) / RATE - self.LEAD_S
            wait = (next_media - media) / self.rate
            await asyncio.sleep(max(0.0005, wait))


# ----------------------------------------------------------------------------- A2DP session
def ldac_config() -> MediaCodecCapabilities:
    return MediaCodecCapabilities(
        media_type=AVDTP_AUDIO_MEDIA_TYPE,
        media_codec_type=A2DP_NON_A2DP_CODEC_TYPE,
        media_codec_information=VendorSpecificMediaCodecInformation(
            LDAC_VENDOR_ID, LDAC_CODEC_ID, bytes([0x10, 0x01])  # 48 kHz, stereo
        ),
    )


def aptx_config(hd: bool) -> MediaCodecCapabilities:
    # 48 kHz (0x10) | stereo (0x02); aptX HD adds 4 reserved bytes
    if hd:
        info = VendorSpecificMediaCodecInformation(APTX_HD_VENDOR_ID, APTX_HD_CODEC_ID,
                                                   bytes([0x12, 0, 0, 0, 0]))
    else:
        info = VendorSpecificMediaCodecInformation(APTX_VENDOR_ID, APTX_CODEC_ID, bytes([0x12]))
    return MediaCodecCapabilities(media_type=AVDTP_AUDIO_MEDIA_TYPE,
                                  media_codec_type=A2DP_NON_A2DP_CODEC_TYPE,
                                  media_codec_information=info)


def sbc_config(max_bitpool: int) -> MediaCodecCapabilities:
    info = SbcMediaCodecInformation
    return MediaCodecCapabilities(
        media_type=AVDTP_AUDIO_MEDIA_TYPE,
        media_codec_type=A2DP_SBC_CODEC_TYPE,
        media_codec_information=SbcMediaCodecInformation(
            sampling_frequency=info.SamplingFrequency.SF_48000,
            channel_mode=info.ChannelMode.JOINT_STEREO,
            block_length=info.BlockLength.BL_16,
            subbands=info.Subbands.S_8,
            allocation_method=info.AllocationMethod.LOUDNESS,
            minimum_bitpool_value=2,
            maximum_bitpool_value=max_bitpool,
        ),
    )


def aac_config(sink_info: AacMediaCodecInformation, bitrate: int) -> MediaCodecCapabilities:
    info = AacMediaCodecInformation
    object_type = (info.ObjectType.MPEG_2_AAC_LC
                   if sink_info.object_type & info.ObjectType.MPEG_2_AAC_LC
                   else info.ObjectType.MPEG_4_AAC_LC)
    if sink_info.bitrate:
        bitrate = min(bitrate, sink_info.bitrate)
    return MediaCodecCapabilities(
        media_type=AVDTP_AUDIO_MEDIA_TYPE,
        media_codec_type=A2DP_MPEG_2_4_AAC_CODEC_TYPE,
        media_codec_information=AacMediaCodecInformation(
            object_type=object_type,
            sampling_frequency=info.SamplingFrequency.SF_48000,
            channels=info.Channels.STEREO,
            vbr=0,
            bitrate=bitrate,
        ),
    )


def aac_sink_usable(sink_info) -> bool:
    info = AacMediaCodecInformation
    return (isinstance(sink_info, AacMediaCodecInformation)
            and bool(sink_info.object_type & (info.ObjectType.MPEG_2_AAC_LC
                                              | info.ObjectType.MPEG_4_AAC_LC))
            and bool(sink_info.sampling_frequency & info.SamplingFrequency.SF_48000)
            and bool(sink_info.channels & info.Channels.STEREO))


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
    headsets: dict = field(default_factory=dict)  # "AA:BB:..": {"name", "codec", "ldac_quality"}


SAVED_FIELDS = ("transport", "address", "codec", "ldac_quality", "sbc_max_bitpool", "aac_bitrate",
                "capture",
                "volume_endpoint", "headsets", "audio_configured", "absolute_volume", "media_keys", "low_latency")


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


def norm_addr(address) -> str:
    """'2c:be:..:2e/P' -> '2C:BE:..:2E'"""
    return str(address).split("/")[0].upper()


AUDIO_VIDEO_MAJOR_CLASS = 0x04


class SonatinaHost:
    def __init__(self, settings: Settings, codec_lib: CodecLib | None = None) -> None:
        self.s = settings
        self.lib = codec_lib or CodecLib()
        self.ring = PcmRing()
        self.volume = EndpointVolume(self.lib, settings.volume_endpoint)
        self.remote = sonatina_avrcp.RemoteControl(self, sonatina_media.sessions())
        self.connection: Connection | None = None
        self.session_task: asyncio.Task | None = None
        self.pump: MediaPump | None = None
        self.state = "idle"
        self.peer: str | None = None          # address of the connected headset
        self.paired: set[str] = set()
        self.pairing_target: str | None = None
        self.pairing_done = asyncio.Event()
        self.pairing_done.set()
        self.discovering = False
        self.discovered: dict[str, str] = {}  # address -> name
        self.headset_delay_ms: float | None = None
        self.on_change = None                 # called after settings changed (save)
        self.ready = False                    # True once powered on and keys loaded

    # -- settings ---------------------------------------------------------------
    def headset_name(self, address: str | None) -> str:
        if not address:
            return "-"
        return self.s.headsets.get(address, {}).get("name") or address

    def codec_for(self, address: str) -> tuple[str, str]:
        h = self.s.headsets.get(address, {})
        return h.get("codec", self.s.codec), h.get("ldac_quality", self.s.ldac_quality)

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self.s)
            except Exception as error:
                log.warning("saving settings failed: %r", error)

    async def refresh_paired(self) -> None:
        self.paired = set()
        if self.device.keystore is not None:
            for name, _keys in await self.device.keystore.get_all():
                self.paired.add(norm_addr(name))
        changed = False
        for address in self.paired:
            if address not in self.s.headsets:
                self.s.headsets[address] = {}
                changed = True
        if not self.s.address and self.paired:
            self.s.address = sorted(self.paired)[0]
            changed = True
        if changed:
            self._changed()

    async def after_power_on(self) -> None:
        """Connectable for paired headsets, never discoverable (nobody else can find us)."""
        await self.device.set_discoverable(False)
        await self.device.set_connectable(True)
        await self.refresh_paired()
        self.ready = True

    # -- device setup ---------------------------------------------------------
    @staticmethod
    def configure_device(device: Device) -> None:
        device.sdp_service_records = {
            0x00010001: make_audio_source_service_sdp_records(0x00010001),
            **sonatina_avrcp.sdp_records(0x00010002, 0x00010003),
        }
        device.pairing_config_factory = lambda connection: PairingConfig(mitm=False)

    def attach(self, device: Device) -> None:
        self.device = device
        self.configure_device(device)
        self.remote.listen(device)
        device.on("connection", self._on_connection)

    def _on_connection(self, connection: Connection) -> None:
        if connection.transport != PhysicalTransport.BR_EDR:
            return
        peer = norm_addr(connection.peer_address)
        loop = asyncio.get_running_loop()
        known = peer in self.paired or peer == self.pairing_target or self.s.allow_unknown
        if not known or self.connection is not None:
            log.warning("refusing %s (%s)", peer, "busy" if known else "not paired")
            loop.create_task(connection.disconnect())
            return
        log.info("connected: %s %s (we are %s)", self.headset_name(peer), peer,
                 "CENTRAL" if connection.role == 0 else "PERIPHERAL")
        self.connection = connection
        self.peer = peer
        connection.on("disconnection", self._on_disconnection)
        self.remote.start(connection)
        self.session_task = loop.create_task(self._session(connection, peer))

    def _on_delay_report(self, delay: int) -> None:
        """AVDTP delay report, in 1/10 ms: the headset's own buffer + decoding."""
        self.headset_delay_ms = delay / 10
        log.info("headset delay %.1f ms", self.headset_delay_ms)

    def latency_ms(self) -> int | None:
        """Estimated delay from Windows to the ears (None while not streaming)."""
        if self.state != "streaming" or self.pump is None or self.pump.encoder is None:
            return None
        return self.pump.latency_ms() + int(self.headset_delay_ms or 0)

    def _on_disconnection(self, reason) -> None:
        log.info("disconnected (reason 0x%02X)", reason)
        self.connection = None
        self.peer = None
        self.state = "idle"
        self.remote.stop()
        if self.pump:
            asyncio.get_running_loop().create_task(self.pump.stop())
        if self.session_task and not self.session_task.done():
            self.session_task.cancel()

    async def _secure(self, connection: Connection) -> None:
        # Lesson from U0: encryption alone hangs on reconnect, authenticate first.
        if not connection.is_encrypted:
            try:
                await connection.authenticate()
            except Exception as error:  # the headset may already be authenticating
                log.debug("authenticate: %s", error)
            if not connection.is_encrypted:
                await connection.encrypt()

    async def _session(self, connection: Connection, peer: str) -> None:
        try:
            self.state = "securing"
            if peer == self.pairing_target:
                await self.pairing_done.wait()   # pair() authenticates this link
            await self._secure(connection)
            if not self.s.headsets.get(peer, {}).get("name"):
                try:
                    name = await asyncio.wait_for(connection.request_remote_name(), 5)
                    self.s.headsets.setdefault(peer, {})["name"] = name
                    self._changed()
                    log.info("headset name: %s", name)
                except Exception as error:
                    log.debug("remote name: %r", error)
            self.state = "negotiating"
            version = await find_avdtp_service_with_connection(connection) or (1, 3)
            protocol = await AvdtpProtocol.connect(connection, version)
            await protocol.discover_remote_endpoints()

            mtu_holder = {"mtu": 679}
            codec, ldac_quality = self.codec_for(peer)
            log.info("headset codecs: %s", ", ".join(self._sink_names(protocol)) or "?")
            choice = None
            for candidate in CODEC_CHAIN[CODEC_CHAIN.index(codec) if codec in CODEC_CHAIN else 0:]:
                choice = self._choose(protocol, candidate, ldac_quality, mtu_holder)
                if choice is not None:
                    break
                log.info("%s not usable with this headset, trying the next codec",
                         CODEC_NAMES[candidate])
            if choice is None:
                raise RuntimeError("no usable sink on the headset (not even SBC)")

            sink, config, make_encoder = choice
            delay_reporting = any(
                c.service_category == AVDTP_DELAY_REPORTING_SERVICE_CATEGORY for c in sink.capabilities)
            self.pump = MediaPump(self.ring, make_encoder, self.volume, self.jitter_target_ms())
            self.headset_delay_ms = None
            source = protocol.add_source(config, self.pump, delay_reporting)
            source.on("delay_report", self._on_delay_report)
            stream = await protocol.create_stream(source, sink)
            await stream.open()
            if stream.rtp_channel is not None:
                mtu_holder["mtu"] = stream.rtp_channel.peer_mtu
                log.info("media channel MTU %d", mtu_holder["mtu"])
            await stream.start()
            self.state = "streaming"
            self.remote.on_streaming()
            if self.s.address != peer:   # the headset in use becomes the active one
                self.s.address = peer
                self._changed()
            await asyncio.get_running_loop().create_future()  # until disconnection
        except asyncio.CancelledError:
            pass
        except Exception as error:
            log.error("session failed: %r", error)
            self.state = "error"
            try:
                await connection.disconnect()
            except Exception:
                pass

    def _choose(self, protocol, codec: str, ldac_quality: str, mtu_holder: dict):
        """(sink, configuration, encoder factory) for `codec`, or None if not usable."""
        mtu = lambda: mtu_holder["mtu"]  # noqa: E731 - known only once the stream is open
        if codec == "ldac":
            sink = protocol.find_remote_sink_by_codec(
                AVDTP_AUDIO_MEDIA_TYPE, A2DP_NON_A2DP_CODEC_TYPE, LDAC_VENDOR_ID, LDAC_CODEC_ID)
            if sink is None:
                return None
            eqmid = LDAC_EQMID.get(ldac_quality, 1)
            return sink, ldac_config(), lambda: LdacEncoder(self.lib, eqmid, mtu())
        if codec in ("aptx_hd", "aptx"):
            hd = codec == "aptx_hd"
            vendor, codec_id = (APTX_HD_VENDOR_ID, APTX_HD_CODEC_ID) if hd else (APTX_VENDOR_ID,
                                                                                APTX_CODEC_ID)
            sink = protocol.find_remote_sink_by_codec(
                AVDTP_AUDIO_MEDIA_TYPE, A2DP_NON_A2DP_CODEC_TYPE, vendor, codec_id)
            info = self._codec_info(sink)
            value = bytes(getattr(info, "value", b"") or b"")
            if sink is None or not self.lib.has_aptx:
                return None
            if value and (value[0] & 0x12) != 0x12:   # 48 kHz + stereo
                log.info("%s sink without 48 kHz stereo (0x%02X)", CODEC_NAMES[codec], value[0])
                return None
            cap = self.max_packet_ms()
            return sink, aptx_config(hd), lambda: AptxEncoder(self.lib, hd, mtu(), cap)
        if codec == "aac":
            sink = protocol.find_remote_sink_by_codec(AVDTP_AUDIO_MEDIA_TYPE,
                                                      A2DP_MPEG_2_4_AAC_CODEC_TYPE)
            info = self._codec_info(sink)
            if sink is None or not aac_sink_usable(info) or not self.lib.has_aac:
                return None
            config = aac_config(info, self.s.aac_bitrate)
            bitrate = config.media_codec_information.bitrate
            return sink, config, lambda: AacEncoder(self.lib, bitrate, mtu())
        sink = protocol.find_remote_sink_by_codec(AVDTP_AUDIO_MEDIA_TYPE, A2DP_SBC_CODEC_TYPE)
        if sink is None:
            return None
        info = self._codec_info(sink)
        remote_max = info.maximum_bitpool_value if isinstance(info, SbcMediaCodecInformation) else 53
        bitpool = max(2, min(self.s.sbc_max_bitpool, remote_max))
        cap = self.max_packet_ms()
        return sink, sbc_config(bitpool), lambda: SbcEncoder(self.lib, bitpool, mtu(), cap)

    def max_packet_ms(self) -> int:
        """Audio waits until its packet is full: smaller packets in low-latency mode."""
        return LOW_LATENCY_PACKET_MS if self.s.low_latency else 0

    def jitter_target_ms(self) -> int:
        return LOW_LATENCY_TARGET_MS if self.s.low_latency else NORMAL_TARGET_MS

    @staticmethod
    def _sink_names(protocol) -> list[str]:
        """Codecs the headset offers, for the log (helps when adding headsets)."""
        names = []
        for endpoint in protocol.remote_endpoints.values():
            for cap in endpoint.capabilities:
                if not isinstance(cap, MediaCodecCapabilities):
                    continue
                info = cap.media_codec_information
                if cap.media_codec_type == A2DP_SBC_CODEC_TYPE:
                    names.append("SBC")
                elif cap.media_codec_type == A2DP_MPEG_2_4_AAC_CODEC_TYPE:
                    names.append("AAC")
                elif isinstance(info, VendorSpecificMediaCodecInformation):
                    key = (info.vendor_id, info.codec_id)
                    names.append(VENDOR_CODECS.get(key, f"vendor 0x{key[0]:04X}/0x{key[1]:04X}"))
                else:
                    names.append(f"type 0x{cap.media_codec_type:02X}")
        return names

    @staticmethod
    def _codec_info(sink):
        if sink is None:
            return None
        for cap in sink.capabilities:
            if isinstance(cap, MediaCodecCapabilities):
                return cap.media_codec_information
        return None

    # -- connection management -------------------------------------------------
    async def keep_connected(self) -> None:
        """Wait a little for a headset to connect to us, then page the preferred one;
        retry forever. Any paired headset that connects by itself is accepted too."""
        await asyncio.sleep(3)
        backoff = 3.0
        while True:
            busy = self.discovering or self.pairing_target is not None
            if self.connection is None and self.s.address and not busy:
                try:
                    log.info("connecting to %s ...", self.headset_name(self.s.address))
                    await self.device.connect(self.s.address, transport=PhysicalTransport.BR_EDR)
                    backoff = 3.0
                except Exception as error:
                    log.info("connect failed (%s), retry in %.0f s", type(error).__name__, backoff)
                    backoff = min(backoff * 1.5, 20.0)
            await asyncio.sleep(backoff if self.connection is None else 2.0)

    # -- headset management (called from the tray) ------------------------------
    async def discover(self, seconds: float = 12.0) -> dict[str, str]:
        """Looks for headsets in pairing mode (Audio/Video class of device)."""
        self.discovering = True
        self.discovered = {}
        unnamed: list = []

        def on_result(address, class_of_device, data, rssi) -> None:
            _services, major, _minor = DeviceClass.split_class_of_device(class_of_device)
            if major != AUDIO_VIDEO_MAJOR_CLASS:
                return
            key = norm_addr(address)
            name = None
            try:
                name = data.get(AdvertisingData.COMPLETE_LOCAL_NAME) or data.get(
                    AdvertisingData.SHORTENED_LOCAL_NAME)
            except Exception:
                pass
            if key not in self.discovered:
                log.info("found %s %s", name or "?", key)
                if not name:
                    unnamed.append(address)
            self.discovered[key] = str(name) if name else self.discovered.get(key, key)

        self.device.on("inquiry_result", on_result)
        try:
            await self.device.start_discovery(auto_restart=False)
            await asyncio.sleep(seconds)
            await self.device.stop_discovery()
            for address in unnamed:
                try:
                    name = await asyncio.wait_for(self.device.request_remote_name(address), 5)
                    self.discovered[norm_addr(address)] = name
                except Exception:
                    pass
        finally:
            self.device.remove_listener("inquiry_result", on_result)
            self.discovering = False
        return dict(self.discovered)

    async def pair(self, address: str, name: str | None = None) -> bool:
        address = norm_addr(address)
        self.pairing_target = address
        self.pairing_done.clear()
        try:
            if self.connection is not None:
                await self.connection.disconnect()
                await asyncio.sleep(1)
            log.info("pairing with %s %s ...", name or "?", address)
            connection = await self.device.connect(address, transport=PhysicalTransport.BR_EDR)
            await connection.authenticate()
            if not connection.is_encrypted:
                await connection.encrypt()
            await self.refresh_paired()
            entry = self.s.headsets.setdefault(address, {})
            if name:
                entry["name"] = name
            self.s.address = address
            self._changed()
            log.info("paired with %s", self.headset_name(address))
            return True
        except Exception as error:
            log.error("pairing with %s failed: %r", address, error)
            return False
        finally:
            self.pairing_target = None
            self.pairing_done.set()

    async def select(self, address: str) -> None:
        """Makes `address` the preferred headset and switches to it."""
        address = norm_addr(address)
        self.s.address = address
        self._changed()
        if self.connection is not None and self.peer != address:
            await self.connection.disconnect()

    async def set_codec(self, address: str, codec: str, ldac_quality: str | None = None) -> None:
        entry = self.s.headsets.setdefault(norm_addr(address), {})
        entry["codec"] = codec
        if ldac_quality:
            entry["ldac_quality"] = ldac_quality
        self._changed()
        if self.connection is not None and self.peer == norm_addr(address):
            await self.connection.disconnect()   # renegotiates with the new codec

    async def set_option(self, name: str, value: bool) -> None:
        """absolute_volume and low_latency need a reconnection to take effect, media_keys not."""
        setattr(self.s, name, value)
        self._changed()
        if name in ("absolute_volume", "low_latency") and self.connection is not None:
            await self.connection.disconnect()

    async def forget(self, address: str) -> None:
        address = norm_addr(address)
        if self.connection is not None and self.peer == address:
            await self.connection.disconnect()
        if self.device.keystore is not None:
            for name, _keys in await self.device.keystore.get_all():
                if norm_addr(name) == address:
                    await self.device.keystore.delete(name)
        self.s.headsets.pop(address, None)
        if self.s.address == address:
            self.s.address = ""
        await self.refresh_paired()
        self._changed()
        log.info("forgot %s", address)

    def audio_age(self) -> float:
        """Seconds since non-silent audio was streamed (AVRCP play/pause logic)."""
        if self.pump is None or not self.pump.last_audio:
            return 1e9
        return time.perf_counter() - self.pump.last_audio

    async def follow_volume(self) -> None:
        while True:
            self.volume.poll()
            self.remote.on_windows_volume()
            await asyncio.sleep(0.1)

    def stats_line(self) -> str:
        p = self.pump
        return (f"[{self.state}] ring={self.ring.level_ms()} ms underruns={self.ring.underruns} "
                f"dropped={self.ring.dropped // FRAME_BYTES} smp"
                + (f" packets={p.packets} skips={p.skips} max_late={p.max_late_ms:.1f} ms"
                   f" margin={p.margin_ms if p.margin_ms is None else round(p.margin_ms, 1)} ms"
                   f" target={p.target_ms} ms rate={(p.rate - 1) * 1e6:+.0f} ppm"
                   f" latency={self.latency_ms()} ms" if p else ""))

    def summary(self) -> str:
        """One line for the tray tooltip."""
        p = self.pump
        if self.state == "streaming" and p is not None and p.encoder is not None:
            vol = "" if self.volume.last is None else (
                tr(" · muto", " · muted") if self.volume.last[0] else f" · vol {self.volume.last[1]}%")
            if self.remote.absolute and vol:
                vol += tr(" (cuffie)", " (headset)")
            latency = self.latency_ms()
            if latency is not None and self.headset_delay_ms is not None:
                vol += tr(" · ritardo ~", " · delay ~") + f"{latency} ms"
            return f"{self.headset_name(self.peer)}: {p.encoder.name} {p.encoder.kbps} kbit/s{vol}"
        if self.discovering:
            return tr("Sonatina: ricerca cuffie...", "Sonatina: looking for headphones...")
        if self.pairing_target:
            return tr("Sonatina: abbinamento con ", "Sonatina: pairing with ") + \
                f"{self.headset_name(self.pairing_target)}..."
        return {"idle": tr("Sonatina: in attesa delle cuffie", "Sonatina: waiting for the headphones"),
                "securing": tr("Sonatina: connessione...", "Sonatina: connecting..."),
                "negotiating": tr("Sonatina: connessione...", "Sonatina: connecting..."),
                "error": tr("Sonatina: errore (vedi log)", "Sonatina: error (see the log)"),
                }.get(self.state, f"Sonatina: {self.state}")

    async def print_stats(self, interval: float = 5.0, to_log: bool = False) -> None:
        last = None
        while True:
            await asyncio.sleep(interval)
            line = self.stats_line()
            if to_log:
                if line != last:
                    log.info("%s", line)
                last = line
            else:
                print(line, flush=True)


# ----------------------------------------------------------------------------- robustness
class SystemResumed(Exception):
    """The PC slept: USB, the radio link and WASAPI streams are stale. Start over."""


async def watch_suspend(interval: float = 2.0, gap: float = 10.0) -> None:
    """A 2 s sleep that takes more than 10 s means the PC was suspended (monotonic time
    keeps counting during sleep on Windows) or the process was frozen."""
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(interval)
        elapsed = time.monotonic() - t0
        if elapsed > gap:
            raise SystemResumed(f"{elapsed:.0f} s")


async def watch_capture(source) -> None:
    while True:
        await asyncio.sleep(1.0)
        source.check()


BARROT_VENDOR_IDS = ("33FA",)


class StrictEventSplitter:
    """HCI event splitter for controllers that pad USB interrupt transfers.

    Barrot (UGREEN BT 6.0, 33FA:0012) sends one spurious byte after the Read Local
    Extended Features event; Bumble's splitter, reading a byte stream, then takes it as
    the start of the next event and every later event is misparsed. Each interrupt
    transfer holds whole events, so bytes that cannot be a complete event at the end of
    a transfer are dropped here."""

    def __init__(self, emit) -> None:
        self.emit = emit
        self.dropped = 0

    def feed(self, data: bytes) -> None:
        data = bytes(data)
        while data:
            if len(data) < 2 or len(data) < 2 + data[1]:
                self.dropped += len(data)
                log.debug("HCI: dropped %d spurious byte(s) %s", len(data), data.hex())
                return
            size = 2 + data[1]
            self.emit(data[:size])
            data = data[size:]


def apply_transport_quirks(spec: str, hci_source) -> None:
    vid = spec.split(":")[1].upper() if spec.lower().startswith("usb:") and spec.count(":") >= 2 else ""
    splitters = getattr(hci_source, "splitters", None)
    if vid in BARROT_VENDOR_IDS and isinstance(splitters, dict):
        from bumble import hci as bumble_hci

        old = splitters.get(bumble_hci.HCI_EVENT_PACKET)
        if old is not None:
            splitters[bumble_hci.HCI_EVENT_PACKET] = StrictEventSplitter(old.emit)
            log.info("Barrot adapter: spurious-byte quirk on")


# ----------------------------------------------------------------------------- main
def set_timer_resolution() -> None:
    if sys.platform == "win32":
        try:
            ctypes.WinDLL("winmm").timeBeginPeriod(1)  # 1 ms sleeps for the media pump
        except OSError:
            pass


async def amain(settings: Settings, on_host=None, stats_to_log: bool = False) -> None:
    """Runs until the dongle goes away (unplugged) or the task is cancelled.
    on_host(host) is called once the host object exists (used by the tray)."""
    from bumble.transport import open_transport

    set_timer_resolution()
    host = SonatinaHost(settings)
    if on_host is not None:
        on_host(host)
    source = ToneSource(host.ring) if settings.tone else CableCapture(host.ring, settings.capture)

    async with await open_transport(settings.transport) as (hci_source, hci_sink):
        apply_transport_quirks(settings.transport, hci_source)
        config = DeviceConfiguration()
        config.name = "Sonatina"
        config.class_of_device = DeviceClass.pack_class_of_device(
            DeviceClass.AUDIO_SERVICE_CLASS,
            DeviceClass.AUDIO_VIDEO_MAJOR_DEVICE_CLASS,
            DeviceClass.AUDIO_VIDEO_UNCATEGORIZED_MINOR_DEVICE_CLASS,
        )
        config.keystore = "JsonKeyStore"
        config.classic_enabled = True
        config.le_enabled = False
        config.classic_sc_enabled = False
        config.classic_smp_enabled = False
        device = Device.from_config_with_hci(config, hci_source, hci_sink)
        host.attach(device)
        await device.power_on()
        await host.after_power_on()
        log.info("Sonatina on %s, paired: %s, preferred: %s", device.public_address,
                 ", ".join(host.headset_name(a) for a in sorted(host.paired)) or "none",
                 host.headset_name(settings.address))
        source.start()
        tasks = [asyncio.create_task(host.keep_connected()),
                 asyncio.create_task(host.follow_volume()),
                 asyncio.create_task(watch_capture(source))]
        if settings.stats:
            tasks.append(asyncio.create_task(host.print_stats(
                interval=60.0 if stats_to_log else 5.0, to_log=stats_to_log)))
        suspend = asyncio.create_task(watch_suspend())
        try:
            await asyncio.wait([asyncio.ensure_future(hci_source.terminated), suspend],
                               return_when=asyncio.FIRST_COMPLETED)
            if suspend.done():
                suspend.result()   # raises SystemResumed
        finally:
            suspend.cancel()
            for t in tasks:
                t.cancel()
            source.stop()
            if host.pump is not None:
                await host.pump.stop()


APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Sonatina")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SonatinaHost: A2DP source on a dedicated USB dongle. "
                    f"Options not given come from {SETTINGS_PATH}.")
    parser.add_argument("--transport")
    parser.add_argument("--address", help="headset to connect (AA:BB:CC:DD:EE:FF)")
    parser.add_argument("--codec", choices=CODEC_CHAIN)
    parser.add_argument("--ldac-quality", choices=["hq", "sq", "mq"])
    parser.add_argument("--sbc-bitpool", type=int)
    parser.add_argument("--capture", help="capture device name (substring)")
    parser.add_argument("--volume-endpoint",
                        help="playback endpoint whose Windows volume is followed (substring)")
    parser.add_argument("--tone", action="store_true", help="1 kHz test tone instead of VB-CABLE")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if not args.debug:
        logging.getLogger("bumble").setLevel(logging.WARNING)

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return

    settings = load_settings(SETTINGS_PATH)
    for name, value in (("transport", args.transport), ("address", args.address),
                        ("codec", args.codec), ("ldac_quality", args.ldac_quality),
                        ("sbc_max_bitpool", args.sbc_bitpool), ("capture", args.capture),
                        ("volume_endpoint", args.volume_endpoint)):
        if value is not None:
            setattr(settings, name, norm_addr(value) if name == "address" else value)
    if args.codec or args.ldac_quality:   # explicit choice wins over the per-headset one
        for entry in settings.headsets.values():
            entry.pop("codec", None)
            entry.pop("ldac_quality", None)
    settings.tone = args.tone
    while True:
        try:
            asyncio.run(amain(settings))
            return
        except SystemResumed as gap:
            log.info("PC resumed from sleep (%s): restarting", gap)
            time.sleep(2.0)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
