"""SBC parameter model: Bluetooth audio capability encoding, frame length and bitrate.

The frame length formula is the one from the SBC specification (also used by libsbc):

    size = 4 + (4 * subbands * channels) / 8
    joint stereo : size += subbands + (blocks * bitpool) / 8
    stereo/dual  : size += (blocks * channels * bitpool) / 8
    mono         : size += (blocks * bitpool) / 8

All divisions are integer divisions, as in the reference C implementation.

The 4-byte "Media Codec Specific Information Elements" block follows the Bluetooth audio
specification (the audio profile specification, 4.3.2):

    octet 0 : sampling frequency (4 bits, MSB first) | channel mode (4 bits)
    octet 1 : block length (4 bits) | subbands (2 bits) | allocation (2 bits)
    octet 2 : minimum bitpool (8 bits)
    octet 3 : maximum bitpool (8 bits)

The default profile therefore encodes as ``11 15 02 35`` (48 kHz, joint stereo,
16 blocks, 8 subbands, loudness, bitpool 2..53), the same bytes seen in real
SBC capability blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

FREQ_BITS = {16000: 0x80, 32000: 0x40, 44100: 0x20, 48000: 0x10}
MODE_BITS = {"mono": 0x08, "dual": 0x04, "stereo": 0x02, "joint": 0x01}
BLOCK_BITS = {4: 0x80, 8: 0x40, 12: 0x20, 16: 0x10}
SUBBAND_BITS = {4: 0x08, 8: 0x04}
ALLOC_CODE = {"snr": 0x02, "loudness": 0x01}

BITS_FREQ = {v: k for k, v in FREQ_BITS.items()}
BITS_MODE = {v: k for k, v in MODE_BITS.items()}
BITS_BLOCK = {v: k for k, v in BLOCK_BITS.items()}
BITS_SUBBAND = {v: k for k, v in SUBBAND_BITS.items()}

CHANNEL_MODES = tuple(MODE_BITS)
ALLOCATIONS = tuple(ALLOC_CODE)


def _pick(table: dict[int, int], octet: int, default: int) -> int:
    """Return the highest supported value among the bits set in `octet`."""
    candidates = [value for bit, value in table.items() if octet & bit]
    return max(candidates) if candidates else default


@dataclass(frozen=True)
class SbcParams:
    sampling: int = 48000
    channel_mode: str = "joint"
    blocks: int = 16
    subbands: int = 8
    allocation: str = "loudness"
    min_bitpool: int = 2
    max_bitpool: int = 53

    def validate(self) -> None:
        if self.sampling not in FREQ_BITS:
            raise ValueError("unsupported SBC sampling frequency %r" % self.sampling)
        if self.channel_mode not in MODE_BITS:
            raise ValueError("unsupported SBC channel mode %r" % self.channel_mode)
        if self.blocks not in BLOCK_BITS:
            raise ValueError("unsupported SBC block length %r" % self.blocks)
        if self.subbands not in SUBBAND_BITS:
            raise ValueError("unsupported SBC subbands %r" % self.subbands)
        if self.allocation not in ALLOC_CODE:
            raise ValueError("unsupported SBC allocation method %r" % self.allocation)
        if not 2 <= self.min_bitpool <= 250:
            raise ValueError(
                "SBC minimum bitpool must be 2..250, got %d" % self.min_bitpool
            )
        if not self.min_bitpool <= self.max_bitpool <= 250:
            raise ValueError(
                "invalid SBC bitpool range %d..%d"
                % (self.min_bitpool, self.max_bitpool)
            )

    @property
    def channels(self) -> int:
        return 1 if self.channel_mode == "mono" else 2

    @property
    def samples_per_frame(self) -> int:
        return self.blocks * self.subbands

    def frame_len(self, bitpool: int | None = None) -> int:
        bp = self.max_bitpool if bitpool is None else bitpool
        ch = self.channels
        size = 4 + (4 * self.subbands * ch) // 8
        if ch == 2 and self.channel_mode == "joint":
            size += self.subbands + (self.blocks * bp) // 8
        elif ch == 2:
            size += (self.blocks * 2 * bp) // 8
        else:
            size += (self.blocks * bp) // 8
        return size

    def frames_per_second(self) -> float:
        return self.sampling / self.samples_per_frame

    def bitrate_kbps(self, bitpool: int | None = None) -> float:
        return self.frame_len(bitpool) * 8 * self.frames_per_second() / 1000.0

    def to_capability(self) -> bytes:
        self.validate()
        octet0 = FREQ_BITS[self.sampling] | MODE_BITS[self.channel_mode]
        octet1 = (
            BLOCK_BITS[self.blocks]
            | SUBBAND_BITS[self.subbands]
            | ALLOC_CODE[self.allocation]
        )
        octet2 = self.min_bitpool & 0xFF
        octet3 = self.max_bitpool & 0xFF
        return bytes([octet0, octet1, octet2, octet3])

    @classmethod
    def from_capability(cls, data: bytes) -> "SbcParams":
        if len(data) < 4:
            raise ValueError("SBC capability must be 4 bytes, got %d" % len(data))
        freq = _pick(BITS_FREQ, data[0] & 0xF0, 44100)
        mode = _pick(BITS_MODE, data[0] & 0x0F, "joint")
        blocks = _pick(BITS_BLOCK, data[1] & 0xF0, 16)
        subbands = _pick(BITS_SUBBAND, data[1] & 0x0C, 8)
        alloc = "snr" if (data[1] & 0x02) else "loudness"
        min_bp = data[2] or 2
        max_bp = data[3] or 53
        if min_bp > max_bp:
            min_bp, max_bp = max_bp, min_bp
        return cls(freq, mode, blocks, subbands, alloc, min_bp, max_bp)

    def clamped_to(self, sink: "SbcParams") -> "SbcParams":
        """Fit the requested parameters into the sink capabilities."""
        max_bp = max(2, min(self.max_bitpool, sink.max_bitpool))
        min_bp = max(2, min(self.min_bitpool, max_bp))
        return replace(
            self,
            sampling=sink.sampling,
            channel_mode=sink.channel_mode,
            blocks=sink.blocks,
            subbands=sink.subbands,
            allocation=sink.allocation,
            min_bitpool=min_bp,
            max_bitpool=max_bp,
        )


def bitrate(params: SbcParams, bitpool: int | None = None) -> float:
    """Bitrate in kbit/s for `params` at `bitpool` (default: maximum)."""
    return params.bitrate_kbps(bitpool)


def bitrate_table(
    params: SbcParams, bitpools: list[int]
) -> list[tuple[int, int, float]]:
    """Return (bitpool, frame_len, kbps) for each bitpool, clamped to the params range."""
    out = []
    for bp in bitpools:
        bp = max(params.min_bitpool, min(params.max_bitpool, bp))
        out.append((bp, params.frame_len(bp), params.bitrate_kbps(bp)))
    return out
