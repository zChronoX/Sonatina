"""Codec backends for Sonatina.

* ``pcm``  : raw little-endian int16, no compression (always available);
* ``sbc``  : correct Bluetooth audio framing with frames of the length given by the SBC
             formula. The frame body is a simple 8-bit quantisation of the PCM
             block (a "carrier"), not a real SBC bitstream: libsbc/AOSP would be
             needed for that. The bitrate is therefore exact;
* ``aac``  : real ADTS bitstream produced by FFmpeg (optional dependency);
* ``aptx`` : real aptX frames produced by FFmpeg (optional dependency).
"""

from __future__ import annotations

import shutil
import struct
import subprocess
from dataclasses import dataclass

import numpy as np

from . import rtp
from .sbc import SbcParams


class CodecError(Exception):
    """Encoding or decoding failed."""


class EncoderUnavailable(CodecError):
    """The requested backend needs an external tool that is not installed."""


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


# --------------------------------------------------------------------------- interface


@dataclass
class Payload:
    data: bytes
    samples: int


class Encoder:
    codec = "?"
    sampling = 48000
    channels = 2

    def payloads(self, pcm: np.ndarray) -> list[Payload]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class Decoder:
    codec = "?"
    sampling = 48000
    channels = 2

    def push(self, payload: bytes) -> None:
        raise NotImplementedError

    def finish(self) -> np.ndarray:
        raise NotImplementedError


# --------------------------------------------------------------------------- PCM


class PcmEncoder(Encoder):
    codec = "pcm"

    def __init__(
        self, sampling: int = 48000, channels: int = 2, samples_per_packet: int = 1024
    ) -> None:
        self.sampling = sampling
        self.channels = channels
        self.samples_per_packet = samples_per_packet

    def payloads(self, pcm: np.ndarray) -> list[Payload]:
        block = self.samples_per_packet
        out = []
        for start in range(0, len(pcm), block):
            chunk = pcm[start : start + block]
            out.append(Payload(chunk.astype("<i2").tobytes(), len(chunk)))
        return out


class PcmDecoder(Decoder):
    codec = "pcm"

    def __init__(self, sampling: int = 48000, channels: int = 2) -> None:
        self.sampling = sampling
        self.channels = channels
        self._buf = bytearray()

    def push(self, payload: bytes) -> None:
        self._buf += payload

    def finish(self) -> np.ndarray:
        data = bytes(self._buf)
        usable = len(data) - (len(data) % (2 * self.channels))
        return (
            np.frombuffer(data[:usable], dtype="<i2").reshape(-1, self.channels).copy()
        )


# --------------------------------------------------------------------------- SBC carrier


class SbcEncoder(Encoder):
    codec = "sbc"

    def __init__(
        self,
        params: SbcParams,
        sampling: int | None = None,
        channels: int = 2,
        bitpool: int | None = None,
        frames_per_packet: int = 3,
    ) -> None:
        params.validate()
        self.params = params
        self.sampling = sampling or params.sampling
        self.channels = channels
        self.bitpool = params.max_bitpool if bitpool is None else bitpool
        self.frames_per_packet = max(1, min(rtp.SBC_MAX_FRAMES, frames_per_packet))
        self.frame_len = params.frame_len(self.bitpool)
        self.samples_per_frame = params.samples_per_frame

    def _frame(self, block: np.ndarray) -> bytes:
        spf = self.samples_per_frame
        if len(block) < spf:
            block = np.pad(block, (0, spf - len(block)))
        count = min(spf, self.frame_len)
        # Frame bodies shorter than the SBC frame are zero padded; longer ones
        # carry the whole block (the remainder is padding).
        index = np.linspace(0, spf - 1, count).astype(np.int32)
        quantised = np.clip(np.round(block[index] / 256.0), -128, 127).astype(np.int8)
        return quantised.tobytes() + bytes(self.frame_len - count)

    def payloads(self, pcm: np.ndarray) -> list[Payload]:
        mono = (
            pcm.astype(np.float32).mean(axis=1)
            if pcm.ndim > 1
            else pcm.astype(np.float32)
        )
        spf = self.samples_per_frame
        frames = [
            self._frame(mono[start : start + spf]) for start in range(0, len(mono), spf)
        ]
        out = []
        for index in range(0, len(frames), self.frames_per_packet):
            group = frames[index : index + self.frames_per_packet]
            out.append(Payload(rtp.pack_sbc(group), len(group) * spf))
        return out


class SbcDecoder(Decoder):
    codec = "sbc"

    def __init__(
        self,
        params: SbcParams,
        sampling: int | None = None,
        channels: int = 2,
        bitpool: int | None = None,
    ) -> None:
        self.params = params
        self.sampling = sampling or params.sampling
        self.channels = channels
        self.bitpool = params.max_bitpool if bitpool is None else bitpool
        self.frame_len = params.frame_len(self.bitpool)
        self.samples_per_frame = params.samples_per_frame
        self._frames: list[bytes] = []

    def push(self, payload: bytes) -> None:
        if len(payload) >= 2 and payload[1] == 0x9C:
            count = payload[0] >> 4
            body = payload[1:]
            if count > 0 and len(body) % count == 0:
                self._frames.extend(rtp.unpack_sbc(payload, len(body) // count))
                return
        self._frames.extend(rtp.unpack_sbc(payload, self.frame_len))

    def finish(self) -> np.ndarray:
        spf = self.samples_per_frame
        blocks = []
        for frame in self._frames:
            n = min(spf, len(frame))
            if n == 0:
                continue
            raw = np.frombuffer(frame[:n], dtype=np.int8).astype(np.float32) * 256.0
            if n == 1:
                block = np.full(spf, float(raw[0]), dtype=np.float32)
            else:
                block = np.interp(
                    np.linspace(0.0, n - 1.0, spf),
                    np.arange(n, dtype=np.float32),
                    raw,
                ).astype(np.float32)
            blocks.append(block)
        if not blocks:
            return np.zeros((0, self.channels), dtype=np.int16)
        mono = np.concatenate(blocks)
        stereo = np.repeat(mono[:, None], self.channels, axis=1)
        return np.clip(stereo, -32768, 32767).astype(np.int16)


# --------------------------------------------------------------------------- FFmpeg codecs


def _run_ffmpeg(args: list[str], data: bytes, what: str) -> bytes:
    exe = ffmpeg_path()
    if exe is None:
        raise EncoderUnavailable(
            "%s needs FFmpeg, which is not installed (winget install --id Gyan.FFmpeg -e)"
            % what
        )
    cmd = [exe, "-hide_banner", "-loglevel", "error"] + args
    result = subprocess.run(cmd, input=data, capture_output=True)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise CodecError(
            "%s failed (exit %d): %s"
            % (what, result.returncode, message or "no output")
        )
    if not result.stdout:
        raise CodecError("%s produced no output" % what)
    return result.stdout


def split_adts(data: bytes) -> list[bytes]:
    """Split an ADTS stream into access units."""
    frames = []
    index = 0
    total = len(data)
    while index + 7 <= total:
        if data[index] != 0xFF or (data[index + 1] & 0xF0) != 0xF0:
            raise CodecError("ADTS sync word not found at offset %d" % index)
        protection_absent = data[index + 1] & 0x01
        frame_length = (
            ((data[index + 3] & 0x03) << 11)
            | (data[index + 4] << 3)
            | (data[index + 5] >> 5)
        )
        header_len = 7 if protection_absent else 9
        if frame_length < header_len:
            raise CodecError("invalid ADTS frame length %d" % frame_length)
        if index + frame_length > total:
            raise CodecError("truncated ADTS frame at offset %d" % index)
        frames.append(data[index : index + frame_length])
        index += frame_length
    if index != total:
        raise CodecError("trailing bytes after the last ADTS frame")
    return frames


class AacEncoder(Encoder):
    codec = "aac"

    def __init__(
        self, sampling: int = 48000, channels: int = 2, bitrate: int = 256
    ) -> None:
        self.sampling = sampling
        self.channels = channels
        self.bitrate = bitrate

    def payloads(self, pcm: np.ndarray) -> list[Payload]:
        raw = pcm.astype("<i2").tobytes()
        encoded = _run_ffmpeg(
            [
                "-f",
                "s16le",
                "-ar",
                str(self.sampling),
                "-ac",
                str(self.channels),
                "-i",
                "pipe:0",
                "-c:a",
                "aac",
                "-b:a",
                "%dk" % self.bitrate,
                "-f",
                "adts",
                "pipe:1",
            ],
            raw,
            "AAC encoding",
        )
        return [Payload(rtp.pack_aac(au), 1024) for au in split_adts(encoded)]


class AacDecoder(Decoder):
    codec = "aac"

    def __init__(self, sampling: int = 48000, channels: int = 2) -> None:
        self.sampling = sampling
        self.channels = channels
        self._aus: list[bytes] = []

    def push(self, payload: bytes) -> None:
        self._aus.append(rtp.unpack_aac(payload))

    def finish(self) -> np.ndarray:
        if not self._aus:
            return np.zeros((0, self.channels), dtype=np.int16)
        encoded = b"".join(self._aus)
        raw = _run_ffmpeg(
            [
                "-f",
                "aac",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-ar",
                str(self.sampling),
                "-ac",
                str(self.channels),
                "pipe:1",
            ],
            encoded,
            "AAC decoding",
        )
        usable = len(raw) - (len(raw) % (2 * self.channels))
        return (
            np.frombuffer(raw[:usable], dtype="<i2").reshape(-1, self.channels).copy()
        )


class AptxEncoder(Encoder):
    codec = "aptx"

    def __init__(
        self, sampling: int = 48000, channels: int = 2, bitrate: int = 352
    ) -> None:
        self.sampling = sampling
        self.channels = channels
        self.bitrate = bitrate
        self.samples_per_frame = 4
        self.bytes_per_frame = 4 if channels == 2 else 2

    def payloads(self, pcm: np.ndarray) -> list[Payload]:
        raw = pcm.astype("<i2").tobytes()
        encoded = _run_ffmpeg(
            [
                "-f",
                "s16le",
                "-ar",
                str(self.sampling),
                "-ac",
                str(self.channels),
                "-i",
                "pipe:0",
                "-c:a",
                "aptx",
                "-strict",
                "experimental",
                "-f",
                "aptx",
                "pipe:1",
            ],
            raw,
            "aptX encoding",
        )
        size = self.bytes_per_frame
        usable = len(encoded) - (len(encoded) % size)
        return [
            Payload(encoded[i : i + size], self.samples_per_frame)
            for i in range(0, usable, size)
        ]


class AptxDecoder(Decoder):
    codec = "aptx"

    def __init__(self, sampling: int = 48000, channels: int = 2) -> None:
        self.sampling = sampling
        self.channels = channels
        self._buf = bytearray()

    def push(self, payload: bytes) -> None:
        self._buf += payload

    def finish(self) -> np.ndarray:
        if not self._buf:
            return np.zeros((0, self.channels), dtype=np.int16)
        raw = _run_ffmpeg(
            [
                "-f",
                "aptx",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-ar",
                str(self.sampling),
                "-ac",
                str(self.channels),
                "pipe:1",
            ],
            bytes(self._buf),
            "aptX decoding",
        )
        usable = len(raw) - (len(raw) % (2 * self.channels))
        return (
            np.frombuffer(raw[:usable], dtype="<i2").reshape(-1, self.channels).copy()
        )


LDAC_MODES = ("mono", "dual", "stereo")
LDAC_BITRATES = {
    0: (909.0, 990.0),  # HQ
    1: (606.0, 660.0),  # SQ
    2: (303.0, 330.0),  # MQ
}


def ldac_bitrate_kbps(sampling: int = 48000, eqmid: int = 0) -> float:
    pair = LDAC_BITRATES.get(eqmid, LDAC_BITRATES[0])
    return pair[0] if sampling in (44100, 88200) else pair[1]


class LdacEncoder(Encoder):
    codec = "ldac"
    samples_per_frame = 128

    def __init__(
        self,
        sampling: int = 48000,
        channels: int = 2,
        eqmid: int = 0,
        frames_per_packet: int = 2,
    ) -> None:
        if sampling not in (44100, 48000, 88200, 96000):
            raise CodecError("unsupported LDAC sampling %d" % sampling)
        if eqmid not in (0, 1, 2):
            raise CodecError("invalid LDAC eqmid %d" % eqmid)
        self.sampling = sampling
        self.channels = channels
        self.eqmid = eqmid
        self.frames_per_packet = max(1, min(15, frames_per_packet))
        kbps = ldac_bitrate_kbps(sampling, eqmid)
        self.frame_len = max(64, int(round(kbps * 1000.0 * 128.0 / (8.0 * sampling))))

    def _frame(self, block: np.ndarray) -> bytes:
        spf = self.samples_per_frame
        if len(block) < spf:
            block = np.pad(block, (0, spf - len(block)))
        count = min(spf, self.frame_len)
        index = np.linspace(0, spf - 1, count).astype(np.int32)
        quantised = np.clip(np.round(block[index] / 256.0), -128, 127).astype(np.int8)
        return quantised.tobytes() + bytes(self.frame_len - count)

    def payloads(self, pcm: np.ndarray) -> list[Payload]:
        mono = (
            pcm.astype(np.float32).mean(axis=1)
            if pcm.ndim > 1
            else pcm.astype(np.float32)
        )
        spf = self.samples_per_frame
        frames = [
            self._frame(mono[start : start + spf]) for start in range(0, len(mono), spf)
        ]
        out = []
        for index in range(0, len(frames), self.frames_per_packet):
            group = frames[index : index + self.frames_per_packet]
            out.append(Payload(rtp.pack_ldac(b"".join(group), len(group)), len(group) * spf))
        return out


class LdacDecoder(Decoder):
    codec = "ldac"
    samples_per_frame = 128

    def __init__(self, sampling: int = 48000, channels: int = 2) -> None:
        self.sampling = sampling
        self.channels = channels
        self._frames: list[bytes] = []

    def push(self, payload: bytes) -> None:
        frame_count, body = rtp.unpack_ldac(payload)
        per = max(1, len(body) // frame_count)
        for i in range(frame_count):
            chunk = body[i * per : (i + 1) * per]
            if chunk:
                self._frames.append(chunk)

    def finish(self) -> np.ndarray:
        spf = self.samples_per_frame
        blocks = []
        for frame in self._frames:
            n = min(spf, len(frame))
            if n == 0:
                continue
            raw = np.frombuffer(frame[:n], dtype=np.int8).astype(np.float32) * 256.0
            if n == 1:
                block = np.full(spf, float(raw[0]), dtype=np.float32)
            else:
                block = np.interp(
                    np.linspace(0.0, n - 1.0, spf),
                    np.arange(n, dtype=np.float32),
                    raw,
                ).astype(np.float32)
            blocks.append(block)
        if not blocks:
            return np.zeros((0, self.channels), dtype=np.int16)
        mono = np.concatenate(blocks)
        stereo = np.repeat(mono[:, None], self.channels, axis=1)
        return np.clip(stereo, -32768, 32767).astype(np.int16)


# --------------------------------------------------------------------------- factory


def make_encoder(
    codec: str,
    sampling: int = 48000,
    channels: int = 2,
    sbc: SbcParams | None = None,
    bitpool: int | None = None,
    aac_bitrate: int = 256,
    frames_per_packet: int = 3,
    ldac_eqmid: int = 0,
) -> Encoder:
    if codec == "pcm":
        return PcmEncoder(sampling, channels)
    if codec == "sbc":
        params = sbc or SbcParams(
            sampling=sampling, channel_mode="joint" if channels == 2 else "mono"
        )
        return SbcEncoder(params, sampling, channels, bitpool, frames_per_packet)
    if codec == "aac":
        return AacEncoder(sampling, channels, aac_bitrate)
    if codec == "aptx":
        return AptxEncoder(sampling, channels)
    if codec == "ldac":
        return LdacEncoder(sampling, channels, eqmid=ldac_eqmid)
    raise CodecError("unknown codec %r" % codec)


def make_decoder(
    codec: str,
    sampling: int = 48000,
    channels: int = 2,
    sbc: SbcParams | None = None,
    bitpool: int | None = None,
) -> Decoder:
    if codec == "pcm":
        return PcmDecoder(sampling, channels)
    if codec == "sbc":
        params = sbc or SbcParams(
            sampling=sampling, channel_mode="joint" if channels == 2 else "mono"
        )
        return SbcDecoder(params, sampling, channels, bitpool)
    if codec == "aac":
        return AacDecoder(sampling, channels)
    if codec == "aptx":
        return AptxDecoder(sampling, channels)
    if codec == "ldac":
        return LdacDecoder(sampling, channels)
    raise CodecError("unknown codec %r" % codec)


def estimate_kbps(
    codec: str,
    sampling: int = 48000,
    channels: int = 2,
    sbc: SbcParams | None = None,
    bitpool: int | None = None,
    aac_bitrate: int = 256,
    ldac_eqmid: int = 0,
) -> float:
    """Nominal bitrate of a profile, before any transmission."""
    if codec == "sbc":
        params = sbc or SbcParams(
            sampling=sampling, channel_mode="joint" if channels == 2 else "mono"
        )
        return params.bitrate_kbps(bitpool)
    if codec == "aac":
        return float(aac_bitrate)
    if codec == "aptx":
        return 352.0
    if codec == "ldac":
        return ldac_bitrate_kbps(sampling, ldac_eqmid)
    return sampling * channels * 16 / 1000.0
