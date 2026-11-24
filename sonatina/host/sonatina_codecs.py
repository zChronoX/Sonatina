"""Encoders (sonatina_codec.dll: LDAC, SBC, AAC, aptX/aptX HD) and the A2DP codec
configurations offered to the headphones."""

from __future__ import annotations

import ctypes
import logging
import os
import sys

from bumble.a2dp import (
    A2DP_MPEG_2_4_AAC_CODEC_TYPE,
    A2DP_NON_A2DP_CODEC_TYPE,
    A2DP_SBC_CODEC_TYPE,
    AacMediaCodecInformation,
    SbcMediaCodecInformation,
    VendorSpecificMediaCodecInformation,
)
from bumble.avdtp import AVDTP_AUDIO_MEDIA_TYPE, MediaCodecCapabilities

from sonatina_common import (
    AAC_FRAME,
    AAC_LATM_OVERHEAD,
    AAC_MIN_ADAPTIVE_BITRATE,
    SBC_MIN_ADAPTIVE_BITPOOL,
    APTX_CODEC_ID,
    APTX_HD_CODEC_ID,
    APTX_HD_VENDOR_ID,
    APTX_VENDOR_ID,
    CHANNELS,
    FRAME_BYTES,
    HERE,
    LDAC_CODEC_ID,
    LDAC_DELAY_MS,
    LDAC_KBPS,
    LDAC_VENDOR_ID,
    PUSH_SAMPLES,
    RATE,
    RTP_HEADER,
    pushes_for,
)

log = logging.getLogger("sonatina")


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
        self.has_msbc = hasattr(L, "sont_msbc_create")   # codec version >= 5 (HFP)
        if self.has_msbc:
            L.sont_msbc_create.restype = ctypes.c_void_p
            L.sont_msbc_create.argtypes = []
            L.sont_msbc_destroy.argtypes = [ctypes.c_void_p]
            L.sont_msbc_encode.restype = ctypes.c_int
            L.sont_msbc_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
            L.sont_msbc_decode.restype = ctypes.c_int
            L.sont_msbc_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
            L.sont_rs_create.restype = ctypes.c_void_p
            L.sont_rs_create.argtypes = [ctypes.c_uint] * 4
            L.sont_rs_destroy.argtypes = [ctypes.c_void_p]
            L.sont_rs_max_out.restype = ctypes.c_uint
            L.sont_rs_max_out.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            L.sont_rs_process.restype = ctypes.c_int
            L.sont_rs_process.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint,
                                          ctypes.c_char_p, ctypes.c_uint]
        self.has_voice = hasattr(L, "sont_voice_create")   # codec version >= 6 (1.2.0)
        if self.has_voice:
            L.sont_voice_create.restype = ctypes.c_void_p
            L.sont_voice_create.argtypes = [ctypes.c_float]
            L.sont_voice_destroy.argtypes = [ctypes.c_void_p]
            L.sont_voice_process.restype = ctypes.c_float
            L.sont_voice_process.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint,
                                             ctypes.c_float, ctypes.c_float]
            L.sont_voice_get.restype = ctypes.c_float
            L.sont_voice_get.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.has_adaptive = hasattr(L, "sont_sbc_set_bitpool")   # codec version >= 7 (1.3.0)
        if self.has_adaptive:
            L.sont_sbc_set_bitpool.restype = ctypes.c_int
            L.sont_sbc_set_bitpool.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            L.sont_aac_set_bitrate.restype = ctypes.c_int
            L.sont_aac_set_bitrate.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            L.sont_eq_create.restype = ctypes.c_void_p
            L.sont_eq_create.argtypes = [ctypes.c_uint]
            L.sont_eq_destroy.argtypes = [ctypes.c_void_p]
            L.sont_eq_set.restype = None
            L.sont_eq_set.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_float]
            L.sont_eq_process.restype = None
            L.sont_eq_process.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]
        self.has_gain = hasattr(L, "sont_pcm_gain")   # codec version >= 2
        if self.has_gain:
            L.sont_pcm_gain.restype = None
            L.sont_pcm_gain.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint,
                                        ctypes.c_int, ctypes.c_int]


# ----------------------------------------------------------------------------- equalizer
EQ_FREQS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
EQ_PRESETS = {                      # dB per band (M4, 1.3.0; the mockup approved 2026-11-23)
    "flat": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "bass": [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    "voice": [-3, -2, -1, 1, 3, 4, 3, 1, 0, -1],
    "treble": [0, 0, 0, 0, 0, 1, 2, 4, 5, 6],
}
EQ_MAX_DB = 12


def eq_gains(entry: dict | None) -> list[int]:
    """The 10 gains a headset's settings ask for (all 0 when off)."""
    entry = entry or {}
    if not entry.get("on"):
        return [0] * len(EQ_FREQS)
    preset = entry.get("preset", "flat")
    if preset == "custom":
        raw = list(entry.get("custom") or [])
        raw = (raw + [0] * len(EQ_FREQS))[:len(EQ_FREQS)]
        return [max(-EQ_MAX_DB, min(EQ_MAX_DB, int(round(float(g))))) for g in raw]
    return list(EQ_PRESETS.get(preset, EQ_PRESETS["flat"]))


class Equalizer:
    """10-band equalizer on the music before encoding (sonatina_codec >= 7). Boosts lower
    the whole signal by the biggest boost (no clipping); filter states survive changes."""

    def __init__(self, lib: CodecLib) -> None:
        self.lib = lib.lib
        self.handle = self.lib.sont_eq_create(RATE)
        if not self.handle:
            raise RuntimeError("equalizer init failed")
        self.gains = [0] * len(EQ_FREQS)
        self.active = False

    @property
    def preamp_db(self) -> int:
        return -max([0] + self.gains)

    def set(self, gains: list[int]) -> None:
        gains = [max(-EQ_MAX_DB, min(EQ_MAX_DB, int(g))) for g in gains][:len(EQ_FREQS)]
        if gains == self.gains:
            return
        self.gains = gains
        self.lib.sont_eq_set(self.handle, (ctypes.c_float * len(EQ_FREQS))(*gains), 0.0)
        self.active = any(gains)

    def process(self, pcm: bytes, frames: int) -> bytes:
        if not self.active:
            return pcm
        buf = ctypes.create_string_buffer(pcm, len(pcm))
        self.lib.sont_eq_process(self.handle, buf, frames)
        return buf.raw

    def close(self) -> None:
        if self.handle:
            self.lib.sont_eq_destroy(self.handle)
            self.handle = None


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
        self.levels = tuple(LDAC_KBPS[i] for i in range(3))   # adaptive quality: 990/660/330

    @property
    def level(self) -> int:
        return self.eqmid

    def set_level(self, level: int) -> bool:
        return self.set_eqmid(level)

    def set_eqmid(self, eqmid: int) -> bool:
        """Changes the quality while streaming (libldac moves to it over a few frames; the
        headphones decode any of the three, nothing to renegotiate)."""
        if eqmid == self.eqmid:
            return True
        if self.lib.sont_ldac_set_eqmid(self.handle, eqmid) != 0:
            return False
        self.eqmid = eqmid
        self.kbps = LDAC_KBPS[eqmid]
        return True

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
        # adaptive quality: the chosen bitpool, about 80 % and 62 % of it (53 -> 42 -> 33)
        self.bitpools = [bitpool]
        if lib.has_adaptive:
            for share in (0.8, 0.62):
                b = max(SBC_MIN_ADAPTIVE_BITPOOL, round(bitpool * share))
                if b < self.bitpools[-1]:
                    self.bitpools.append(b)
        self.levels = tuple(self.lib.sont_sbc_frame_bytes(RATE, 3, 16, 8, 1, b) * 8 * RATE // 128 // 1000
                            for b in self.bitpools)
        self.level = 0

    def set_level(self, level: int) -> bool:
        """A new bitpool from the next frame on: every SBC frame carries its own bitpool,
        the headphones follow without renegotiation (like Android's SBC bitpool steps)."""
        if level == self.level:
            return True
        if not 0 <= level < len(self.bitpools):
            return False
        size = self.lib.sont_sbc_set_bitpool(self.handle, self.bitpools[level])
        if size <= 0:
            return False
        # frames already waiting keep the old bitpool: each SBC frame header carries its own
        self.level = level
        self.bitpool = self.bitpools[level]
        self.frame_bytes = size
        self.kbps = self.levels[level]
        return True

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
        # adaptive quality: the chosen bitrate, 3/4 and 1/2 of it, never under 128 kbit/s
        self.bitrates = [self.bitrate]
        if lib.has_adaptive:
            for share in (0.75, 0.5):
                b = max(AAC_MIN_ADAPTIVE_BITRATE, int(self.bitrate * share) // 1000 * 1000)
                if b < self.bitrates[-1]:
                    self.bitrates.append(b)
        self.levels = tuple(b // 1000 for b in self.bitrates)
        self.level = 0

    def set_level(self, level: int) -> bool:
        if level == self.level:
            return True
        if not 0 <= level < len(self.bitrates):
            return False
        if self.lib.sont_aac_set_bitrate(self.handle, self.bitrates[level]) != 0:
            return False
        self.level = level
        self.bitrate = self.bitrates[level]
        self.kbps = self.levels[level]
        return True

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
