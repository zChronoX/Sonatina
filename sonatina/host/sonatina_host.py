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
    A2DP_NON_A2DP_CODEC_TYPE,
    A2DP_SBC_CODEC_TYPE,
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
RTP_HEADER = 12
SSRC = 0x534F4E54                     # "SONT"


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

    def __init__(self, lib: CodecLib, bitpool: int, mtu: int) -> None:
        self.lib = lib.lib
        self.bitpool = bitpool
        self.handle = self.lib.sont_sbc_create(RATE, 3, 16, 8, 1, bitpool)
        if not self.handle:
            raise RuntimeError("SBC encoder init failed")
        self.frame_bytes = self.lib.sont_sbc_frame_bytes(RATE, 3, 16, 8, 1, bitpool)
        self.per_packet = max(1, min(15, (mtu - RTP_HEADER - 1) // self.frame_bytes))
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
    def __init__(self, ring: PcmRing, freq: float = 1000.0) -> None:
        self.ring = ring
        self.freq = freq
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        n = 0
        t0 = time.perf_counter()
        while True:
            due = int((time.perf_counter() - t0) * RATE)
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

    def __init__(self, ring: PcmRing, device_name: str) -> None:
        import sounddevice as sd

        self.sd = sd
        self.ring = ring
        index, dev, api = find_capture_device(device_name)
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

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("capture status: %s", status)
        self.ring.write(bytes(indata))

    def start(self) -> None:
        self.stream.start()

    def stop(self) -> None:
        self.stream.stop()


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
    Duck-typed for bumble.avdtp.LocalSource (start/stop)."""

    LEAD_S = 0.020
    MAX_BACKLOG_S = 0.200

    def __init__(self, ring: PcmRing, make_encoder, volume: EndpointVolume | None = None) -> None:
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

    async def start(self, rtp_channel) -> None:
        await self.stop()
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

    async def _run(self, channel) -> None:
        pcm_bytes = PUSH_SAMPLES * FRAME_BYTES
        t0 = time.perf_counter()
        done = 0
        while True:
            now = time.perf_counter()
            target = int((now - t0 + self.LEAD_S) * RATE)
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
                if not_silent(pcm):
                    self.last_audio = now
                if self.volume is not None:
                    pcm = self.volume.apply(pcm, PUSH_SAMPLES)
                for payload, samples in self.encoder.push(pcm):
                    header = struct.pack(">BBHII", 0x80, 96, self.seq & 0xFFFF,
                                         self.timestamp & 0xFFFFFFFF, SSRC)
                    try:
                        channel.write(header + payload)
                    except Exception as error:   # link dropped under us: stop quietly
                        log.debug("media channel closed: %r", error)
                        return
                    self.seq += 1
                    self.timestamp += samples
                    self.packets += 1
                done += PUSH_SAMPLES
            next_due = t0 + (done + PUSH_SAMPLES) / RATE - self.LEAD_S
            await asyncio.sleep(max(0.0005, next_due - time.perf_counter()))


# ----------------------------------------------------------------------------- A2DP session
def ldac_config() -> MediaCodecCapabilities:
    return MediaCodecCapabilities(
        media_type=AVDTP_AUDIO_MEDIA_TYPE,
        media_codec_type=A2DP_NON_A2DP_CODEC_TYPE,
        media_codec_information=VendorSpecificMediaCodecInformation(
            LDAC_VENDOR_ID, LDAC_CODEC_ID, bytes([0x10, 0x01])  # 48 kHz, stereo
        ),
    )


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


@dataclass
class Settings:
    transport: str = "usb:0BDA:8771"
    address: str = ""             # preferred headset ("" = the first paired one)
    codec: str = "ldac"           # default codec: ldac | sbc (per headset override in `headsets`)
    ldac_quality: str = "sq"      # hq | sq | mq
    sbc_max_bitpool: int = 53
    capture: str = "CABLE Output"
    volume_endpoint: str = "VB-Audio Virtual Cable"
    tone: bool = False
    stats: bool = True
    allow_unknown: bool = False   # accept connections from headsets we never paired
    audio_configured: bool = False  # VB-CABLE already made the default output once
    absolute_volume: bool = True  # AVRCP: volume on the headset when it supports it
    media_keys: bool = True       # AVRCP: headset buttons -> Windows media keys
    headsets: dict = field(default_factory=dict)  # "AA:BB:..": {"name", "codec", "ldac_quality"}


SAVED_FIELDS = ("transport", "address", "codec", "ldac_quality", "sbc_max_bitpool", "capture",
                "volume_endpoint", "headsets", "audio_configured", "absolute_volume", "media_keys")


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
            choice = None
            codec, ldac_quality = self.codec_for(peer)
            if codec == "ldac":
                sink = protocol.find_remote_sink_by_codec(
                    AVDTP_AUDIO_MEDIA_TYPE, A2DP_NON_A2DP_CODEC_TYPE, LDAC_VENDOR_ID, LDAC_CODEC_ID)
                if sink is not None:
                    eqmid = LDAC_EQMID.get(ldac_quality, 1)
                    choice = (sink, ldac_config(),
                              lambda: LdacEncoder(self.lib, eqmid, mtu_holder["mtu"]))
                else:
                    log.warning("headset has no LDAC sink: falling back to SBC")
            if choice is None:
                sink = protocol.find_remote_sink_by_codec(AVDTP_AUDIO_MEDIA_TYPE, A2DP_SBC_CODEC_TYPE)
                if sink is None:
                    raise RuntimeError("no SBC sink on the headset")
                remote_max = 53
                for cap in sink.capabilities:
                    if isinstance(cap, MediaCodecCapabilities) and isinstance(
                            cap.media_codec_information, SbcMediaCodecInformation):
                        remote_max = cap.media_codec_information.maximum_bitpool_value
                bitpool = max(2, min(self.s.sbc_max_bitpool, remote_max))
                choice = (sink, sbc_config(bitpool),
                          lambda: SbcEncoder(self.lib, bitpool, mtu_holder["mtu"]))

            sink, config, make_encoder = choice
            delay_reporting = any(
                c.service_category == AVDTP_DELAY_REPORTING_SERVICE_CATEGORY for c in sink.capabilities)
            self.pump = MediaPump(self.ring, make_encoder, self.volume)
            source = protocol.add_source(config, self.pump, delay_reporting)
            source.on("delay_report", lambda d: log.info("headset delay %.1f ms", d / 10))
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
        """absolute_volume needs a reconnection to take effect, media_keys does not."""
        setattr(self.s, name, value)
        self._changed()
        if name == "absolute_volume" and self.connection is not None:
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
                + (f" packets={p.packets} skips={p.skips} max_late={p.max_late_ms:.1f} ms" if p else ""))

    def summary(self) -> str:
        """One line for the tray tooltip."""
        p = self.pump
        if self.state == "streaming" and p is not None and p.encoder is not None:
            vol = "" if self.volume.last is None else (
                tr(" · muto", " · muted") if self.volume.last[0] else f" · vol {self.volume.last[1]}%")
            if self.remote.absolute and vol:
                vol += tr(" (cuffie)", " (headset)")
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
                 asyncio.create_task(host.follow_volume())]
        if settings.stats:
            tasks.append(asyncio.create_task(host.print_stats(
                interval=60.0 if stats_to_log else 5.0, to_log=stats_to_log)))
        try:
            await hci_source.terminated
        finally:
            for t in tasks:
                t.cancel()
            source.stop()


APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Sonatina")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SonatinaHost: A2DP source on a dedicated USB dongle. "
                    f"Options not given come from {SETTINGS_PATH}.")
    parser.add_argument("--transport")
    parser.add_argument("--address", help="headset to connect (AA:BB:CC:DD:EE:FF)")
    parser.add_argument("--codec", choices=["ldac", "sbc"])
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
    try:
        asyncio.run(amain(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
