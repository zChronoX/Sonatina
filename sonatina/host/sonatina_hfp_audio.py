"""Voice audio of a call: SCO <-> Windows, the microphone cable (split from sonatina_hfp
in 1.3.0)."""

from __future__ import annotations

import array
import ctypes
import logging
import os
import sys
import struct
import threading
import time

log = logging.getLogger("sonatina.hfp")

from sonatina_hfp_common import (
    H2_SYNC,
    MIC_DEVICES,
    MSBC_FRAME,
    MSBC_SAMPLES,
    RATE,
)
import sonatina_hfp_usb

# ----------------------------------------------------------------------------- PCM helpers
class ByteRing:
    """Thread-safe FIFO with prefill, for the microphone output (48 kHz stereo int16)."""

    def __init__(self, frame_bytes: int, rate: int, prefill_ms: int = 40, max_ms: int = 160):
        self.lock = threading.Lock()
        self.buf = bytearray()
        self.frame = frame_bytes
        self.prefill = rate * prefill_ms // 1000 * frame_bytes
        self.max = rate * max_ms // 1000 * frame_bytes
        self.playing = False
        self.underruns = 0
        self.dropped = 0

    def write(self, data: bytes) -> None:
        with self.lock:
            self.buf += data
            if len(self.buf) > self.max:
                drop = len(self.buf) - self.prefill
                drop -= drop % self.frame
                del self.buf[:drop]
                self.dropped += drop

    def read(self, n: int) -> bytes:
        with self.lock:
            if not self.playing:
                if len(self.buf) < self.prefill:
                    return bytes(n)
                self.playing = True
            if len(self.buf) < n:
                self.playing = False
                self.underruns += 1
                out = bytes(self.buf) + bytes(n - len(self.buf))
                self.buf.clear()
                return out
            out = bytes(self.buf[:n])
            del self.buf[:n]
            return out

    def clear(self) -> None:
        with self.lock:
            self.buf.clear()
            self.playing = False


class Resampler:
    def __init__(self, lib, in_rate: int, out_rate: int, in_ch: int, out_ch: int) -> None:
        self.lib = lib
        self.h = lib.sont_rs_create(in_rate, out_rate, in_ch, out_ch)
        if not self.h:
            raise RuntimeError("resampler")
        self.in_frame = 2 * in_ch
        self.out_frame = 2 * out_ch

    def process(self, pcm: bytes) -> bytes:
        frames = len(pcm) // self.in_frame
        cap = self.lib.sont_rs_max_out(self.h, frames)
        out = ctypes.create_string_buffer(cap * self.out_frame)
        n = self.lib.sont_rs_process(self.h, pcm, frames, out, cap)
        if n < 0:
            raise RuntimeError("resampler overflow")
        return out.raw[:n * self.out_frame]

    def close(self) -> None:
        if self.h:
            self.lib.sont_rs_destroy(self.h)
            self.h = None


def fit_frames(block: bytes, have: int, want: int, frame: int = 4) -> bytes:
    """Drops or repeats single frames, evenly spaced, so that `have` frames become `want`
    (a few per 480 at most: the clock servo; inaudible after the voice-band filter)."""
    if have == want or have <= 0:
        return block
    n = abs(have - want)
    cuts = [have * (i + 1) // (n + 1) for i in range(n)]
    out = bytearray()
    prev = 0
    for c in cuts:
        if have > want:                     # drop frame c
            out += block[prev * frame:c * frame]
            prev = c + 1
        else:                               # repeat frame c
            out += block[prev * frame:(c + 1) * frame]
            out += block[c * frame:(c + 1) * frame]
            prev = c + 1
    out += block[prev * frame:]
    return bytes(out)


def peak(pcm: bytes) -> int:
    import array
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    return max((abs(v) for v in a), default=0)


# ----------------------------------------------------------------------------- SCO dump
class ScoDump:
    """Raw record of the first seconds of a call, for offline analysis (mSBC debugging).
    On with the environment variable SONATINA_SCO_DUMP=<seconds>. Text file in the app
    folder: 'R t status hex' received SCO packets, 'T t hex' sent ones, 'I t index status
    actual slot' USB isochronous IN packets."""

    def __init__(self, codec: str, seconds: float) -> None:
        self.codec = codec
        self.seconds = seconds
        self.t0 = time.monotonic()
        self.lines: list[str] = []
        self.done = False
        sonatina_hfp_usb.ISO_TRACE = []           # the USB reader fills it

    @staticmethod
    def seconds_from_env() -> float:
        try:
            return float(os.environ.get("SONATINA_SCO_DUMP", "0") or 0)
        except ValueError:
            return 0.0

    def active(self) -> bool:
        if self.done:
            return False
        if time.monotonic() - self.t0 > self.seconds:
            self.write()
            return False
        return True

    def rx(self, status: int, data: bytes) -> None:
        if self.active():
            self.lines.append(f"R {time.monotonic() - self.t0:.4f} {status} {data.hex()}")

    def tx(self, data: bytes) -> None:
        if self.active():
            self.lines.append(f"T {time.monotonic() - self.t0:.4f} {data.hex()}")

    def write(self) -> None:
        if self.done:
            return
        self.done = True
        trace, sonatina_hfp_usb.ISO_TRACE = sonatina_hfp_usb.ISO_TRACE or [], None
        lines = self.lines + [f"I {t - self.t0:.4f} {i} {st} {n} {slot}" for t, i, st, n, slot in trace
                              if t >= self.t0]
        try:
            import sonatina_host
            path = os.path.join(sonatina_host.APP_DIR, f"sco_dump_{self.codec}_{time.strftime('%H%M%S')}.txt")
            with open(path, "w", encoding="ascii") as f:
                f.write("\n".join(lines) + "\n")
            log.info("SCO dump: %d lines -> %s", len(lines), path)
        except Exception as error:
            log.warning("SCO dump not written: %r", error)


# ----------------------------------------------------------------------------- SCO audio
class ScoAudio:
    """Both directions of an open SCO link.

    rx: SCO payload -> (mSBC frames ->) 8/16 kHz PCM -> 48 kHz mono -> voice cleaning
        (1.2.0: RNNoise, automatic level, gate, limiter; 10 ms frames) -> stereo -> mic_sink
    tx: Windows audio ring (48 kHz stereo) -> volume -> 8/16 kHz mono -> (mSBC ->) SCO,
        exactly as many bytes as were received (the headset paces the link).

    Clock servo (tx): the headset's SCO clock and the Windows audio clock drift apart, and
    the call starts with whatever A2DP left in the ring, so without help the ring grew to
    120 ms during calls (2026-11-20). Once a second the lowest ring level is compared with
    TX_TARGET_MS (PI control, like the A2DP pump) and the number of Windows samples taken per 10 ms block is changed by up
    to 0.5 % (a few cents of pitch, inaudible on 8/16 kHz voice). Far above the target
    (call start) the excess is cut at once.

    Gaps (tx top-up): normally one packet goes out per packet received, which follows the
    headset clock exactly. Incoming packets are sometimes lost or held back for 50-150 ms
    (2026-11-20), and then nothing would go out while the headset keeps playing. With the
    USB lane, when it is about to go idle and nothing has arrived for GAP_S, a packet is
    made on the spot (paced by the USB frames, the right rate); packets arriving late
    later are not sent once the lane already holds MAX_BACKLOG of them. A first version
    that topped up by time over 2 s windows overfilled the lane (370 drops)."""

    TX_BLOCK = 480        # 10 ms of 48 kHz pulled from the Windows ring at a time
    TX_TARGET_MS = 10     # margin aimed at: lowest Windows ring level left after a read
    TX_PPM_PER_MS = 100   # servo gain: 10 ms of error -> 0.1 %
    TX_PPM_PER_MS_S = 3   # integral: learns the steady clock offset (critically damped)
    TX_MAX_PPM = 5000     # +-0.5 %
    TX_TRIM_MS = 80       # above this the excess is dropped at once
    GAP_S = 0.030         # nothing received for this long: the lane asks for packets
    MAX_BACKLOG = 8       # packets queued in the lane beyond the 8 in flight (24 ms)
    LIMIT = 29000         # microphone ceiling after gain (headsets peak at 30000 raw, 2026-11-20)

    VOICE_FRAME = 480     # 10 ms at 48 kHz: the unit of the voice cleaning (RNNoise)
    STRENGTH = {"normal": 0.85, "strong": 1.0}   # share of the noise RNNoise removes

    def __init__(self, lib, codec: str, ring, volume, mic_sink, mic_gain_db: float, send,
                 lane=None, settings=None) -> None:
        self.lib = lib
        self.codec = codec
        self.ring = ring
        self.volume = volume
        self.mic_sink = mic_sink
        self.send = send
        self.rate = 16000 if codec == "msbc" else 8000
        self.gain_q16 = int(65536 * 10 ** (mic_gain_db / 20))
        self.cur_gain = self.gain_q16     # limiter: lowered at once on peaks, back slowly
        self.limited = 0
        self.level = 0.0                  # microphone level 0..1 for the dashboard (decays)
        self.up = Resampler(lib, self.rate, RATE, 1, 1)
        self.settings = settings          # mic_denoise / mic_agc / mic_gate / mic_gain_db, read live
        self.mic_gain_db = mic_gain_db
        self.voice = lib.sont_voice_create(float(mic_gain_db)) if hasattr(lib, "sont_voice_create") else None
        self.voice_buf = bytearray()      # 48 kHz mono waiting for a full 10 ms frame
        self.level_in = 0.0               # level before the cleaning (dashboard "before")
        self.vad = 0.0                    # voice probability (RNNoise)
        self.agc_db = mic_gain_db
        self.voice_frames = 0
        self.voice_flags = 0
        self.down = Resampler(lib, RATE, self.rate, 2, 1)
        self.msbc = lib.sont_msbc_create() if codec == "msbc" else None
        self.rx_buf = bytearray()
        self.rx_seq: int | None = None  # mSBC: H2 sequence number expected next (0..3)
        self.rx_gap_s = 0.0             # silence on the link before the current packet
        self.frames_missing = 0         # mSBC frames that never arrived (concealed)
        self.tx_buf = bytearray()       # coded bytes ready for the link
        self.tx_pcm = bytearray()       # mSBC: 16 kHz samples waiting for a full frame
        self.rx_packets = 0
        self.rx_bytes = 0
        self.rx_bad_status = 0
        self.frames_ok = 0
        self.frames_lost = 0
        self.tx_packets = 0
        self.tx_peak = 0                # Windows audio sent to the headset (sampled)
        self.sync_lost = 0
        self.decode_errors = 0
        self.mic_peak = 0
        self.frames_empty = 0           # mSBC: stretches of zeros (link not sending yet)
        self.started = time.monotonic()
        self.closed = False
        self.dump: ScoDump | None = None
        self.tx_ppm = 0.0               # servo: speed of the Windows side vs. the headset
        self.tx_drift = 0.0             # servo integral term (ppm)
        self.tx_phase = 0.0             # fractional samples carried between blocks
        self.tx_underruns = getattr(ring, "underruns", 0)
        self.tx_trims = 0
        self.tx_servo_at = time.monotonic() + 1.0
        self.byte_rate = 8000 if codec == "msbc" else 16000   # SCO air rate, both directions
        self.lane = lane
        self.last_rx = time.monotonic()
        self.packet_len = 0             # size of the incoming packets (= the outgoing ones)
        self.handle = None
        self.topups = 0                 # packets made by the lane during gaps
        self.held = 0                   # packets not sent: the lane was already full
        if lane is not None:
            lane.on_idle = self._idle_packet
        self.silent_pulls = 0           # 10 ms blocks of silence: the Windows ring was empty
        self.win = self._new_window()

    # -- receive ---------------------------------------------------------------
    def on_packet(self, packet) -> None:
        if self.closed:
            return
        data = bytes(packet.data)
        now = time.monotonic()
        if data:
            self.rx_gap_s = max(self.rx_gap_s, now - self.last_rx)
        self.rx_packets += 1
        self.rx_bytes += len(data)
        if self.dump is not None:
            self.dump.rx(getattr(packet, "packet_status", 0), data)
        if getattr(packet, "packet_status", 0):
            self.rx_bad_status += 1
        try:
            self._receive(data, getattr(packet, "packet_status", 0))
        except Exception as error:   # never let audio errors reach the HCI layer
            log.debug("sco rx: %r", error)
        if data:
            self.last_rx = now
            self.packet_len = len(data)
            self.handle = getattr(packet, "connection_handle", None)
            self.win["rx"] += 1
            if self.lane is not None and self.lane.backlog() >= self.MAX_BACKLOG:
                self.held += 1                  # late burst: the gap was already filled
                self.win["held"] += 1
            else:
                self._transmit(len(data), self.handle)

    def _receive(self, data: bytes, status: int) -> None:
        if self.codec == "msbc":
            pcm = self._decode_msbc(data)
        else:
            pcm = data[: len(data) // 2 * 2]
        if not pcm:
            return
        mono = self.up.process(pcm)
        if not mono:
            return
        self.voice_buf += mono
        size = self.VOICE_FRAME * 2
        while len(self.voice_buf) >= size:
            frame = bytes(self.voice_buf[:size])
            del self.voice_buf[:size]
            frame = self._clean(frame) if self.voice is not None else self._gain(frame)
            stereo = array.array("h", bytes(len(frame) * 2))
            samples = array.array("h")
            samples.frombytes(frame)
            stereo[0::2] = samples
            stereo[1::2] = samples
            self.mic_sink(stereo.tobytes())

    def voice_mode(self) -> tuple[int, float]:
        """(flags, strength) for sont_voice_process from the settings."""
        s = self.settings
        if s is None:
            return 0, 1.0
        mode = str(getattr(s, "mic_denoise", "off"))
        flags = (1 if mode in self.STRENGTH else 0) | (2 if getattr(s, "mic_agc", False) else 0) \
            | (4 if getattr(s, "mic_gate", False) else 0)
        return flags, self.STRENGTH.get(mode, 1.0)

    def _clean(self, frame: bytes) -> bytes:
        """One 10 ms frame through the cleaning in the codec library (gain and limiter
        included: without a flag only those)."""
        flags, strength = self.voice_mode()
        gain_db = float(getattr(self.settings, "mic_gain_db", self.mic_gain_db))
        buf = ctypes.create_string_buffer(frame, len(frame))
        lib = self.lib
        self.vad = lib.sont_voice_process(self.voice, buf, flags, strength, gain_db)
        self.voice_flags = flags
        self.voice_frames += 1
        self.level_in = max(lib.sont_voice_get(self.voice, 3), self.level_in * 0.9)
        out_peak = lib.sont_voice_get(self.voice, 4)
        self.agc_db = lib.sont_voice_get(self.voice, 1)
        if lib.sont_voice_get(self.voice, 5) < 0.99:
            self.limited += 1
        level = int(out_peak * 32767)
        if level > self.mic_peak:
            self.mic_peak = level
        self.level = max(out_peak, self.level * 0.9)
        return buf.raw

    def _gain(self, out: bytes) -> bytes:
        """Fixed gain and limiter in Python (codec library without the voice functions)."""
        samples = array.array("h")
        samples.frombytes(out)
        level = max(max(samples), -min(samples))
        self.level_in = max(level / 32767, self.level_in * 0.9)
        target = self.gain_q16
        if level * target > self.LIMIT << 16:         # would clip: just enough gain, now
            target = max(self.gain_q16 // 4, (self.LIMIT << 16) // level)
            self.limited += 1
        if target > self.cur_gain:                     # release: about 6 dB in 0.5 s
            target = min(target, self.cur_gain + max(1, self.cur_gain // 150))
        if (target != 65536 or self.cur_gain != 65536) and self.lib_has_gain():
            buf = ctypes.create_string_buffer(out, len(out))
            self.lib.sont_pcm_gain(buf, len(out) // 2, 1, self.cur_gain, target)
            out = buf.raw
        self.cur_gain = target
        level = level * target >> 16
        if level > self.mic_peak:
            self.mic_peak = level
        self.level = max(level / 32767, self.level * 0.9)
        return out

    def lib_has_gain(self) -> bool:
        return hasattr(self.lib, "sont_pcm_gain")

    def _missing_frames(self, seq: int) -> int:
        """mSBC frames that never arrived before this one: the H2 sequence number says how
        many modulo 4, the time without packets says how many times 4 more (long gaps)."""
        expected, self.rx_seq = self.rx_seq, (seq + 1) % 4
        if expected is None:
            return 0
        short = (seq - expected) % 4
        gap, self.rx_gap_s = self.rx_gap_s, 0.0
        # packets come in USB bursts ~24 ms apart; beyond that, 30 ms per 4 frames missing
        laps = max(0, round((gap - 0.024 - short * 0.0075) / 0.030)) if gap > 0.060 else 0
        return min(short + 4 * laps, 40)

    def _decode_msbc(self, data: bytes) -> bytes:
        buf = self.rx_buf
        buf += data
        out = bytearray()
        pcm = ctypes.create_string_buffer(MSBC_SAMPLES * 2)
        while len(buf) >= MSBC_FRAME:
            if buf[0] == 0x01 and buf[1] in H2_SYNC and buf[2] == 0xAD:
                for _ in range(self._missing_frames(H2_SYNC.index(buf[1]))):
                    self.lib.sont_msbc_decode(self.msbc, None, pcm)     # concealment
                    self.frames_missing += 1
                    out += pcm.raw
                ok = self.lib.sont_msbc_decode(self.msbc, bytes(buf[2:59]), pcm) == 0
                if ok:
                    self.frames_ok += 1
                else:
                    self.frames_lost += 1
                    self.decode_errors += 1
                out += pcm.raw
                del buf[:MSBC_FRAME]
                continue
            # lost sync: find the next header, conceal what was skipped
            idx = -1
            for i in range(1, len(buf) - 2):
                if buf[i] == 0x01 and buf[i + 1] in H2_SYNC and buf[i + 2] == 0xAD:
                    idx = i
                    break
            skipped = len(buf) - 2 if idx < 0 else idx
            empty = not any(buf[:skipped])
            del buf[:skipped]
            if not empty:
                self.sync_lost += 1
            lost = max(1, round(skipped / MSBC_FRAME)) if skipped >= MSBC_FRAME // 2 else 0
            if lost and self.rx_seq is not None:
                self.rx_seq = (self.rx_seq + lost) % 4   # those frames are concealed below
            for _ in range(lost):
                self.lib.sont_msbc_decode(self.msbc, None, pcm)
                if empty:
                    self.frames_empty += 1
                else:
                    self.frames_lost += 1
                out += pcm.raw
        return bytes(out)

    # -- transmit --------------------------------------------------------------
    def _idle_packet(self) -> bytes | None:
        """The USB lane would go idle: during a gap in what arrives, a packet made now
        (HCI SCO payload: handle, length, data). None otherwise."""
        if self.closed or self.handle is None or not self.packet_len:
            return None
        if time.monotonic() - self.last_rx < self.GAP_S:
            return None
        data = self._produce(self.packet_len)
        self.topups += 1
        self.win["topups"] += 1
        return struct.pack("<HB", self.handle & 0x0FFF, len(data)) + data

    @staticmethod
    def _new_window() -> dict:
        return {"t0": time.monotonic(), "rx": 0, "topups": 0, "held": 0, "silent": 0,
                "ring_min": None, "ring_max": 0}

    def window_stats(self) -> str:
        """What happened since the last call (rates and extremes), for the periodic log."""
        w, self.win = self.win, self._new_window()
        secs = max(0.001, time.monotonic() - w["t0"])
        nominal = self.byte_rate / 24 if self.rx_bytes == 0 else self.byte_rate * self.rx_packets / self.rx_bytes
        return (f"last {secs:.0f} s: rx {w['rx'] / secs:.0f}/{nominal:.0f} pkt/s, top-ups {w['topups']},"
                f" held {w['held']},"
                f" silent blocks {w['silent']}, windows ring {w['ring_min']}-{w['ring_max']} ms")

    def _servo(self) -> None:
        """Once a second: speed the Windows side up or down to keep its ring near the target."""
        take_min = getattr(self.ring, "take_min_ms", None)
        if take_min is None:               # test rings: no servo
            return
        now = time.monotonic()
        if now < self.tx_servo_at:
            return
        self.tx_servo_at = now + 1.0
        low = take_min()                   # lowest fill left after a read: the safety margin
        underruns = getattr(self.ring, "underruns", 0)
        if underruns != self.tx_underruns:  # ran dry: no margin at all
            self.tx_underruns = underruns
            low = 0.0
        if low is None:                    # no read yet (first second): the fill itself
            level = getattr(self.ring, "level_ms", None)
            if level is None:
                return
            low = float(level())
        trim = getattr(self.ring, "trim_ms", None)
        if low > self.TX_TRIM_MS and trim is not None:
            trim(self.TX_TARGET_MS + self.TX_BLOCK * 1000 // RATE)
            self.tx_trims += 1
            low = self.TX_TARGET_MS
        error = low - self.TX_TARGET_MS
        limit = self.TX_MAX_PPM
        self.tx_drift = max(-limit, min(limit, self.tx_drift + error * self.TX_PPM_PER_MS_S))
        self.tx_ppm = max(-limit, min(limit, self.tx_drift + error * self.TX_PPM_PER_MS))

    def _pull_windows(self) -> bytes:
        """About 10 ms of Windows audio (a few samples more or less: servo), at the link
        rate, mono."""
        self._servo()
        self.tx_phase += self.TX_BLOCK * self.tx_ppm / 1e6
        extra = int(self.tx_phase)
        self.tx_phase -= extra
        frames = self.TX_BLOCK + extra
        block = self.ring.read(frames * 4)
        if getattr(self.ring, "playing", True) is False:    # empty: silence was returned
            self.silent_pulls += 1
            self.win["silent"] += 1
        level = getattr(self.ring, "level_ms", None)
        if level is not None:
            ms = level()
            w = self.win
            w["ring_min"] = ms if w["ring_min"] is None else min(w["ring_min"], ms)
            w["ring_max"] = max(w["ring_max"], ms)
        if self.volume is not None:
            block = self.volume.apply(block, frames)
        if frames != self.TX_BLOCK:
            # the link asks for a fixed amount of audio: squeeze the block back to 10 ms,
            # otherwise taking more samples would only mean fewer pulls (no speed change)
            block = fit_frames(block, frames, self.TX_BLOCK)
        out = self.down.process(block)
        self._pulls = getattr(self, "_pulls", 0) + 1
        if self._pulls % 8 == 1:
            p = peak(out)
            if p > self.tx_peak:
                self.tx_peak = p
        return out

    def _transmit(self, n: int, handle) -> None:
        out = self._produce(n)
        try:
            self.send(out)
            self.tx_packets += 1
        except Exception as error:
            log.debug("sco tx: %r", error)

    def _produce(self, n: int) -> bytes:
        """The next n bytes for the link (encoded Windows audio)."""
        if self.codec == "msbc":
            pcm = ctypes.create_string_buffer(MSBC_SAMPLES * 2)
            frame = ctypes.create_string_buffer(MSBC_FRAME)
            while len(self.tx_buf) < n:
                while len(self.tx_pcm) < MSBC_SAMPLES * 2:
                    self.tx_pcm += self._pull_windows()
                ctypes.memmove(pcm, bytes(self.tx_pcm[: MSBC_SAMPLES * 2]), MSBC_SAMPLES * 2)
                del self.tx_pcm[: MSBC_SAMPLES * 2]
                if self.lib.sont_msbc_encode(self.msbc, pcm, frame) != MSBC_FRAME:
                    break
                self.tx_buf += frame.raw
        else:
            while len(self.tx_buf) < n:
                self.tx_buf += self._pull_windows()
        out = bytes(self.tx_buf[:n])
        del self.tx_buf[:n]
        if self.dump is not None:
            self.dump.tx(out)
        return out

    # -- status ----------------------------------------------------------------
    def frame_quality(self) -> float | None:
        """Share of good mSBC frames (None for CVSD or before any frame)."""
        total = self.frames_ok + self.frames_lost
        if self.codec != "msbc" or total == 0:
            return None
        return self.frames_ok / total

    def stats(self) -> str:
        secs = max(0.001, time.monotonic() - self.started)
        q = self.frame_quality()
        return (f"{self.codec} rx {self.rx_packets / secs:.0f} pkt/s ({self.rx_bytes / secs / 1000:.1f} kB/s)"
                f" tx {self.tx_packets} bad-status {self.rx_bad_status}"
                + (f" frames ok {self.frames_ok} lost {self.frames_lost} ({q:.0%}, sync {self.sync_lost},"
                   f" crc {self.decode_errors}, empty {self.frames_empty}, missing {self.frames_missing})"
                   if q is not None else "")
                + f" mic peak {self.mic_peak} (limited {self.limited}) out peak {self.tx_peak}"
                + f" servo {self.tx_ppm:+.0f} ppm trims {self.tx_trims} top-ups {self.topups} held {self.held}"
                + f" silent blocks {self.silent_pulls}"
                + (f" voice {self.voice_summary()}" if self.voice is not None else ""))

    def voice_summary(self) -> str:
        flags = self.voice_flags
        parts = [("rnnoise" if flags & 1 else "no-nr"), (f"agc {self.agc_db:+.0f} dB" if flags & 2
                                                         else f"gain {self.agc_db:+.0f} dB")]
        if flags & 4:
            parts.append("gate")
        return ",".join(parts)

    def close(self) -> None:
        self.closed = True
        if self.lane is not None and self.lane.on_idle == self._idle_packet:
            self.lane.on_idle = None
        if self.dump is not None:
            self.dump.write()
        self.up.close()
        self.down.close()
        if self.voice:
            self.lib.sont_voice_destroy(self.voice)
            self.voice = None
        if self.msbc:
            self.lib.sont_msbc_destroy(self.msbc)
            self.msbc = None


# ----------------------------------------------------------------------------- Windows side
def _endpoints(flow: str):
    """[(name, IMMDevice)] of the active endpoints, flow 'render' or 'capture'."""
    import sonatina_host
    sonatina_host.com_init()
    from pycaw.constants import DEVICE_STATE, EDataFlow
    from pycaw.pycaw import AudioUtilities

    enumerator = AudioUtilities.GetDeviceEnumerator()
    data_flow = EDataFlow.eRender.value if flow == "render" else EDataFlow.eCapture.value
    coll = enumerator.EnumAudioEndpoints(data_flow, DEVICE_STATE.ACTIVE.value)
    out = []
    for i in range(coll.GetCount()):
        dev = coll.Item(i)
        out.append((AudioUtilities.CreateDevice(dev).FriendlyName or "", dev))
    return out


def find_mic_cable(preferred: str = "") -> str | None:
    """Substring identifying the virtual cable used as microphone (both its ends match)."""
    if sys.platform != "win32":
        return preferred or None
    try:
        render = [n for n, _d in _endpoints("render")]
        capture = [n for n, _d in _endpoints("capture")]
    except Exception as error:
        log.debug("endpoints: %r", error)
        return None
    for key in ([preferred] if preferred else []) + list(MIC_DEVICES):
        k = key.lower()
        if any(k in n.lower() for n in render) and any(k in n.lower() for n in capture):
            return key
    return None


class MicWatcher:
    """Is some app recording from the virtual microphone? (Windows capture sessions)"""

    IGNORED = ("systemsettings.exe",)   # the input level meter of Settings > Sound

    def __init__(self, key: str) -> None:
        self.key = key
        self.device = None
        self.name = None
        self.warned = False
        self.last_apps: tuple = ()

    def _open(self) -> None:
        for name, dev in _endpoints("capture"):
            if self.key.lower() in name.lower():
                self.device, self.name = dev, name
                log.info("microphone watch: \"%s\"", name)
                return
        raise RuntimeError(f'no recording endpoint matching "{self.key}"')

    def active_apps(self) -> list[str]:
        """Names of the processes with an active session on the endpoint."""
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioSessionControl2, IAudioSessionManager2

        if self.device is None:
            self._open()
        try:
            mgr = self.device.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
            mgr = mgr.QueryInterface(IAudioSessionManager2)
            sessions = mgr.GetSessionEnumerator()
            apps = []
            for i in range(sessions.GetCount()):
                ctl = sessions.GetSession(i)
                if ctl is None or ctl.GetState() != 1:      # AudioSessionStateActive
                    continue
                name = "?"
                try:
                    pid = ctl.QueryInterface(IAudioSessionControl2).GetProcessId()
                    name = _process_name(pid)
                except Exception:
                    pass
                apps.append(name)
            return apps
        except Exception:
            self.device = None        # endpoint gone (cable reinstalled): find it again
            raise

    def poll(self) -> list[str] | None:
        try:
            apps = self.active_apps()
            self.warned = False
        except Exception as error:
            if not self.warned:
                self.warned = True
                log.warning("cannot watch the microphone (%r)", error)
            return None
        key = tuple(sorted(apps))
        if key != self.last_apps:
            self.last_apps = key
            log.info("microphone in use by: %s", ", ".join(key) if key else "nobody")
        return [a for a in apps if a.lower() not in self.IGNORED]


def _process_name(pid: int) -> str:
    if not pid:
        return "system"
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return f"pid {pid}"


class MicOutput:
    """Plays the headset microphone into the virtual cable ("Hi-Fi Cable Input")."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.ring = ByteRing(4, RATE)
        self.stream = None
        self.name = None

    def start(self) -> None:
        import sounddevice as sd

        best = None
        for index, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] < 1 or self.key.lower() not in dev["name"].lower():
                continue
            api = sd.query_hostapis(dev["hostapi"])["name"]
            if "WASAPI" in api:
                best = (index, dev, api)
                break
            if best is None:
                best = (index, dev, api)
        if best is None:
            raise RuntimeError(f'no playback device matching "{self.key}"')
        index, dev, api = best
        extra = None
        if "WASAPI" in api:
            try:
                extra = sd.WasapiSettings(auto_convert=True)
            except TypeError:
                extra = sd.WasapiSettings()
        self.ring.clear()
        self.stream = sd.RawOutputStream(device=index, samplerate=RATE, channels=2, dtype="int16",
                                         blocksize=RATE // 100, latency="low",
                                         callback=self._callback, extra_settings=extra)
        self.stream.start()
        self.name = dev["name"]
        log.info("microphone out: [%d] %s (%s)", index, dev["name"], api)

    def _callback(self, outdata, frames, time_info, status) -> None:
        outdata[:] = self.ring.read(frames * 4)

    def write(self, pcm: bytes) -> None:
        self.ring.write(pcm)

    def stop(self) -> None:
        if self.stream is not None:
            try:
                self.stream.abort()
                self.stream.close()
            except Exception as error:
                log.debug("mic out close: %r", error)
            self.stream = None
