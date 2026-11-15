"""RTP framing for Bluetooth audio media packets.

Only the subset needed by the simulator is implemented:

* a fixed 12-byte RTP header (version 2, no CSRC, no extension, no padding);
* payload type 96 (dynamic), clock 90 kHz;
* payload layouts for SBC, AAC and raw codecs as described in the audio profile specification.

SBC: the high nibble of the first payload byte carries the number of SBC frames;
     the low nibble is unused (RFA).
AAC: a 16-bit AU-headers-length field followed by one AU header (13-bit AU-size,
     3-bit AU-index) and the ADTS frame. One AU per packet.
PCM/aptX: the encoded frame(s) follow the RTP header directly.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

RTP_VERSION = 2
RTP_HEADER_LEN = 12
PAYLOAD_TYPE = 96
CLOCK_RATE = 90_000
MAX_SEQ = 0x10000
MAX_TS = 0x1_0000_0000

SBC_MAX_FRAMES = 15


class RtpError(ValueError):
    pass


@dataclass
class RtpPacket:
    seq: int = 0
    timestamp: int = 0
    ssrc: int = 0
    marker: bool = False
    payload_type: int = PAYLOAD_TYPE
    payload: bytes = b""

    def encode(self) -> bytes:
        if self.seq < 0 or self.timestamp < 0:
            raise RtpError("negative sequence or timestamp")
        b0 = RTP_VERSION << 6
        b1 = (0x80 if self.marker else 0x00) | (self.payload_type & 0x7F)
        header = struct.pack(
            "!BBHII",
            b0,
            b1,
            self.seq % MAX_SEQ,
            self.timestamp % MAX_TS,
            self.ssrc & 0xFFFFFFFF,
        )
        return header + self.payload

    @classmethod
    def decode(cls, data: bytes) -> "RtpPacket":
        if len(data) < RTP_HEADER_LEN:
            raise RtpError("RTP packet too short (%d bytes)" % len(data))
        b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", data[:RTP_HEADER_LEN])
        version = b0 >> 6
        if version != RTP_VERSION:
            raise RtpError("unsupported RTP version %d" % version)
        if b0 & 0x10:
            raise RtpError("RTP header extension not supported")
        csrc = b0 & 0x0F
        offset = RTP_HEADER_LEN + 4 * csrc
        if len(data) < offset:
            raise RtpError("RTP packet truncated in CSRC list")
        pad = b0 & 0x20
        end = len(data)
        if pad:
            pad_len = data[-1] if data else 0
            if pad_len == 0 or pad_len > len(data) - offset:
                raise RtpError("invalid RTP padding length")
            end -= pad_len
        return cls(
            seq=seq,
            timestamp=ts,
            ssrc=ssrc,
            marker=bool(b1 & 0x80),
            payload_type=b1 & 0x7F,
            payload=data[offset:end],
        )


# --------------------------------------------------------------------------- payloads


@dataclass
class MediaPayload:
    """One RTP payload plus the number of PCM samples it carries."""

    data: bytes
    samples: int = field(default=0)


def pack_sbc(frames: list[bytes]) -> bytes:
    if not 1 <= len(frames) <= SBC_MAX_FRAMES:
        raise RtpError("SBC payload must carry 1..15 frames, got %d" % len(frames))
    return bytes([len(frames) << 4]) + b"".join(frames)


def unpack_sbc(payload: bytes, frame_len: int) -> list[bytes]:
    if len(payload) < 1:
        raise RtpError("empty SBC payload")
    count = payload[0] >> 4
    if count == 0:
        raise RtpError("SBC payload declares 0 frames")
    body = payload[1:]
    if frame_len <= 0:
        raise RtpError("invalid SBC frame length %d" % frame_len)
    if len(body) < count * frame_len:
        raise RtpError(
            "SBC payload truncated: %d bytes for %d frames of %d"
            % (len(body), count, frame_len)
        )
    return [body[i * frame_len : (i + 1) * frame_len] for i in range(count)]


def pack_aac(au: bytes, index: int = 0) -> bytes:
    if not au:
        raise RtpError("empty AAC access unit")
    if len(au) > 0x1FFF:
        raise RtpError("AAC access unit too large for a 13-bit AU-size")
    header = ((len(au) & 0x1FFF) << 3) | (index & 0x07)
    return struct.pack("!HH", 16, header) + au


def unpack_aac(payload: bytes) -> bytes:
    if len(payload) < 4:
        raise RtpError("AAC payload too short")
    headers_len = struct.unpack("!H", payload[:2])[0]
    if headers_len != 16:
        raise RtpError(
            "only one AAC AU per packet is supported (headers_len=%d)" % headers_len
        )
    header = struct.unpack("!H", payload[2:4])[0]
    size = header >> 3
    body = payload[4:]
    if size == 0 or len(body) < size:
        raise RtpError(
            "AAC payload truncated (size=%d, available=%d)" % (size, len(body))
        )
    return body[:size]


def pack_raw(data: bytes) -> bytes:
    if not data:
        raise RtpError("empty payload")
    return data


def unpack_raw(payload: bytes) -> bytes:
    if not payload:
        raise RtpError("empty payload")
    return payload


def timestamp_after(samples: int, sampling: int, clock: int = CLOCK_RATE) -> int:
    """RTP timestamp advance for `samples` PCM samples at `sampling` Hz."""
    if sampling <= 0:
        raise RtpError("invalid sampling frequency %d" % sampling)
    return (samples * clock) // sampling


class SequenceTracker:
    """Count lost packets from RTP sequence numbers."""

    def __init__(self) -> None:
        self.expected: int | None = None
        self.lost = 0
        self.seen = 0

    def feed(self, seq: int) -> int:
        gap = 0
        if self.expected is not None:
            gap = (seq - self.expected) % MAX_SEQ
            if gap > MAX_SEQ // 2:
                # Out-of-order packet: ignore it for loss accounting.
                return 0
        self.expected = (seq + 1) % MAX_SEQ
        self.seen += 1
        self.lost += gap
        return gap
