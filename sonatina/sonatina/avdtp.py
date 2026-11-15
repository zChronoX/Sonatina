"""AVDTP signalling subset (the audio profile specification, section 8).

Implemented messages: Discover, GetCapabilities, SetConfiguration, Open, Start,
Suspend, Close and Abort, plus the capability encoding for the Media Transport
and Media Codec service categories (SBC, AAC and non-Bluetooth audio codecs).

The bytes on the wire follow the specification so the exchanges can be compared
with a real capture; the transport underneath is UDP loopback, not L2CAP.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .sbc import SbcParams

# Packet types (2 bits)
PT_SINGLE = 0
PT_START = 1
PT_CONTINUE = 2
PT_END = 3

# Message types (2 bits)
MT_COMMAND = 0
MT_GENERAL_REJECT = 1
MT_ACCEPT = 2
MT_REJECT = 3

# Signal identifiers (6 bits)
SIG_DISCOVER = 0x01
SIG_GET_CAPABILITIES = 0x02
SIG_SET_CONFIGURATION = 0x03
SIG_GET_CONFIGURATION = 0x04
SIG_RECONFIGURE = 0x05
SIG_OPEN = 0x06
SIG_START = 0x07
SIG_CLOSE = 0x08
SIG_SUSPEND = 0x09
SIG_ABORT = 0x0A

SIGNAL_NAMES = {
    SIG_DISCOVER: "Discover",
    SIG_GET_CAPABILITIES: "GetCapabilities",
    SIG_SET_CONFIGURATION: "SetConfiguration",
    SIG_GET_CONFIGURATION: "GetConfiguration",
    SIG_RECONFIGURE: "Reconfigure",
    SIG_OPEN: "Open",
    SIG_START: "Start",
    SIG_CLOSE: "Close",
    SIG_SUSPEND: "Suspend",
    SIG_ABORT: "Abort",
}

# Service categories
CAT_MEDIA_TRANSPORT = 1
CAT_REPORTING = 2
CAT_RECOVERY = 3
CAT_CONTENT_PROTECTION = 4
CAT_HEADER_COMPRESSION = 5
CAT_MULTIPLEXING = 6
CAT_MEDIA_CODEC = 7
CAT_DELAY_REPORTING = 8

CATEGORY_NAMES = {
    CAT_MEDIA_TRANSPORT: "MediaTransport",
    CAT_REPORTING: "Reporting",
    CAT_RECOVERY: "Recovery",
    CAT_CONTENT_PROTECTION: "ContentProtection",
    CAT_HEADER_COMPRESSION: "HeaderCompression",
    CAT_MULTIPLEXING: "Multiplexing",
    CAT_MEDIA_CODEC: "MediaCodec",
    CAT_DELAY_REPORTING: "DelayReporting",
}

# Error codes used in reject responses
ERR_BAD_HEADER_FORMAT = 0x01
ERR_BAD_LENGTH = 0x11
ERR_BAD_ACP_SEID = 0x12
ERR_SEP_IN_USE = 0x13
ERR_SEP_NOT_IN_USE = 0x14
ERR_BAD_SERVICE_CATEGORY = 0x19
ERR_INVALID_CAPABILITIES = 0x1A

MEDIA_TYPE_AUDIO = 0
TSEP_SRC = 0
TSEP_SNK = 1

CODEC_TYPE = {"sbc": 0x00, "aac": 0x02, "pcm": 0xFF, "aptx": 0xFF}
CODEC_BY_TYPE = {0x00: "sbc", 0x02: "aac", 0xFF: None}

AAC_OBJECT_TYPE_AAC_LC = 2
AAC_FREQ_INDEX = {
    96000: 0,
    88200: 1,
    64000: 2,
    48000: 3,
    44100: 4,
    32000: 5,
    24000: 6,
    22050: 7,
    16000: 8,
    12000: 9,
    11025: 10,
    8000: 11,
    7350: 12,
}
AAC_FREQ_BY_INDEX = {v: k for k, v in AAC_FREQ_INDEX.items()}


class AvdtpError(Exception):
    """Protocol violation or rejected message."""


class SignalTimeout(AvdtpError):
    """No (matching) response arrived before the deadline."""


class UnsupportedCodec(AvdtpError):
    """The peer does not advertise the requested codec."""


@dataclass
class AacInfo:
    object_type: int = AAC_OBJECT_TYPE_AAC_LC
    sampling: int = 48000
    channels: int = 2
    vbr: bool = False
    bitrate: int = 256  # kbit/s

    def to_bytes(self) -> bytes:
        freq_index = AAC_FREQ_INDEX.get(self.sampling, 3)
        value = self.object_type & 0x3F
        value = (value << 4) | (freq_index & 0x0F)
        value = (value << 4) | (self.channels & 0x0F)
        value = (value << 1) | (1 if self.vbr else 0)
        value = (value << 23) | (max(0, self.bitrate) & 0x7FFFFF)
        return value.to_bytes(5, "big")

    @classmethod
    def from_bytes(cls, data: bytes) -> "AacInfo":
        if len(data) < 5:
            raise ValueError("AAC capability must be 5 bytes, got %d" % len(data))
        value = int.from_bytes(data[:5], "big")
        bitrate = value & 0x7FFFFF
        value >>= 23
        vbr = bool(value & 0x01)
        value >>= 1
        channels = value & 0x0F
        value >>= 4
        freq_index = value & 0x0F
        object_type = (value >> 4) & 0x3F
        return cls(
            object_type=object_type,
            sampling=AAC_FREQ_BY_INDEX.get(freq_index, 48000),
            channels=channels or 2,
            vbr=vbr,
            bitrate=bitrate,
        )


@dataclass
class CodecCapability:
    codec: str
    sbc: SbcParams | None = None
    aac: AacInfo | None = None

    def specific_bytes(self) -> bytes:
        if self.codec == "sbc":
            return (self.sbc or SbcParams()).to_capability()
        if self.codec == "aac":
            return (self.aac or AacInfo()).to_bytes()
        if self.codec == "aptx":
            # Non-Bluetooth audio codec: two vendor specific octets (placeholder vendor id).
            return b"\x00\x00"
        return b""


@dataclass
class SepInfo:
    seid: int
    in_use: bool = False
    tsep: int = TSEP_SNK
    media_type: int = MEDIA_TYPE_AUDIO

    def to_bytes(self) -> bytes:
        if not 1 <= self.seid <= 62:
            raise AvdtpError("SEID must be 1..62, got %d" % self.seid)
        first = (0x80 if self.in_use else 0) | ((self.seid & 0x3F) << 1)
        second = ((self.media_type & 0x0F) << 4) | ((self.tsep & 0x03) << 2)
        return bytes([first, second])

    @classmethod
    def from_bytes(cls, data: bytes) -> "SepInfo":
        if len(data) < 2:
            raise AvdtpError("SEP info must be 2 bytes")
        seid = (data[0] >> 1) & 0x3F
        if seid == 0:
            raise AvdtpError("invalid SEID 0")
        return cls(
            seid=seid,
            in_use=bool(data[0] & 0x80),
            media_type=(data[1] >> 4) & 0x0F,
            tsep=(data[1] >> 2) & 0x03,
        )


@dataclass
class Message:
    transaction: int = 0
    packet_type: int = PT_SINGLE
    message_type: int = MT_COMMAND
    signal: int = 0
    payload: bytes = b""

    def encode(self) -> bytes:
        header = bytes(
            [
                ((self.transaction & 0x0F) << 4)
                | ((self.packet_type & 0x03) << 2)
                | (self.message_type & 0x03),
                (self.signal & 0x3F) << 2,
            ]
        )
        return header + self.payload

    @classmethod
    def decode(cls, data: bytes) -> "Message":
        if len(data) < 2:
            raise AvdtpError("AVDTP message too short (%d bytes)" % len(data))
        first, second = data[0], data[1]
        if (second & 0x03) != 0:
            raise AvdtpError("AVDTP RFA bits are not zero")
        return cls(
            transaction=(first >> 4) & 0x0F,
            packet_type=(first >> 2) & 0x03,
            message_type=first & 0x03,
            signal=(second >> 2) & 0x3F,
            payload=data[2:],
        )

    def reject(self, error_code: int, transaction: int | None = None) -> "Message":
        return Message(
            transaction=self.transaction if transaction is None else transaction,
            message_type=MT_REJECT,
            signal=self.signal,
            payload=bytes([error_code & 0xFF]),
        )

    def accept(self, payload: bytes = b"") -> "Message":
        return Message(
            transaction=self.transaction,
            message_type=MT_ACCEPT,
            signal=self.signal,
            payload=payload,
        )

    @property
    def name(self) -> str:
        return SIGNAL_NAMES.get(self.signal, "0x%02X" % self.signal)


# --------------------------------------------------------------------------- capabilities


def capability(category: int, payload: bytes = b"") -> bytes:
    """Encode one service capability: ``category``, ``length``, then the payload.

    The header is the AVDTP ``avdtp_service_capability`` structure: two full
    bytes (category and length), not the 6-bit/2-bit packing used by SDP.
    """
    if not 0 <= category <= 0xFF:
        raise AvdtpError("service category must fit in one byte, got %d" % category)
    if len(payload) > 0xFF:
        raise AvdtpError("capability payload must be at most 255 bytes")
    return bytes([category & 0xFF, len(payload)]) + payload


def encode_capabilities(cap: CodecCapability) -> bytes:
    out = capability(CAT_MEDIA_TRANSPORT)
    # Media Codec data: octet 0 is media type (high nibble) with RFA (low
    # nibble), octet 1 is the full Media Codec Type (0xFF = non-Bluetooth audio), then
    # the codec specific information elements.
    header = bytes([(MEDIA_TYPE_AUDIO & 0x0F) << 4, CODEC_TYPE[cap.codec] & 0xFF])
    out += capability(CAT_MEDIA_CODEC, header + cap.specific_bytes())
    return out


@dataclass
class CapabilitySet:
    codec: str
    sbc: SbcParams | None = None
    aac: AacInfo | None = None
    categories: list[int] = field(default_factory=list)


def parse_capabilities(data: bytes) -> CapabilitySet:
    index = 0
    categories: list[int] = []
    codec: str | None = None
    sbc: SbcParams | None = None
    aac: AacInfo | None = None
    seen_codec_block = False
    while index < len(data):
        if index + 2 > len(data):
            raise AvdtpError("capability header truncated")
        category = data[index]
        length = data[index + 1]
        index += 2
        if index + length > len(data):
            raise AvdtpError("capability block truncated")
        body = data[index : index + length]
        index += length
        categories.append(category)
        if category == CAT_MEDIA_TRANSPORT:
            if length != 0:
                raise AvdtpError(
                    "Media Transport capability must have length 0, got %d" % length
                )
        if category == CAT_MEDIA_CODEC:
            if seen_codec_block:
                raise AvdtpError("duplicate Media Codec capability")
            seen_codec_block = True
            if len(body) < 2:
                raise AvdtpError(
                    "Media Codec capability is too short (%d bytes)" % len(body)
                )
            media_type = (body[0] >> 4) & 0x0F
            codec_type = body[1]
            if media_type != MEDIA_TYPE_AUDIO:
                raise AvdtpError("unsupported media type %d" % media_type)
            if codec_type == 0x00:
                if len(body) < 6:
                    raise AvdtpError(
                        "SBC capability is too short (%d bytes)" % len(body)
                    )
                sbc = SbcParams.from_capability(body[2:6])
                codec = "sbc"
            elif codec_type == 0x02:
                if len(body) < 7:
                    raise AvdtpError(
                        "AAC capability is too short (%d bytes)" % len(body)
                    )
                aac = AacInfo.from_bytes(body[2:7])
                codec = "aac"
            elif codec_type == 0xFF:
                # Non-Bluetooth audio codec: the presence of vendor specific information
                # distinguishes raw PCM (header only) from aptX (vendor info).
                codec = "pcm" if len(body) == 2 else "aptx"
            else:
                raise AvdtpError("unsupported codec type 0x%02X" % codec_type)
    if codec is None:
        raise AvdtpError("no Media Codec capability found")
    return CapabilitySet(codec=codec, sbc=sbc, aac=aac, categories=categories)


def parse_sep_list(payload: bytes) -> list[SepInfo]:
    if len(payload) % 2:
        raise AvdtpError("SEP list length is not a multiple of 2")
    return [SepInfo.from_bytes(payload[i : i + 2]) for i in range(0, len(payload), 2)]


def encode_sep_list(seps: list[SepInfo]) -> bytes:
    return b"".join(sep.to_bytes() for sep in seps)


# --------------------------------------------------------------------------- client helpers


def command(signal: int, payload: bytes = b"", transaction: int = 0) -> Message:
    return Message(
        transaction=transaction, message_type=MT_COMMAND, signal=signal, payload=payload
    )


def set_configuration(int_seid: int, acp_seid: int, cap: CodecCapability) -> bytes:
    return bytes([acp_seid & 0x3F, int_seid & 0x3F]) + encode_capabilities(cap)


def single_seid(seid: int) -> bytes:
    return bytes([seid & 0x3F])


def find_codec(
    caps: list[tuple[SepInfo, CapabilitySet]], wanted: str
) -> tuple[SepInfo, CapabilitySet] | None:
    for sep, cap in caps:
        if cap.codec == wanted:
            return sep, cap
    return None
