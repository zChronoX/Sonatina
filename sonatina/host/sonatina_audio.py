"""Windows side and media timing: the jitter buffer (PcmRing), VB-CABLE capture, the
Windows volume, the default output, and the media pump that paces encoding and writes
A2DP packets (with the slow-radio limit and adaptive LDAC)."""

from __future__ import annotations

import array
import asyncio
import ctypes
import logging
import math
import re
import struct
import sys
import threading
import time

import sonatina_hfp
from sonatina_codecs import CodecLib
from sonatina_common import CHANNELS, FRAME_BYTES, PUSH_SAMPLES, RATE, SSRC, com_init

log = logging.getLogger("sonatina")


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

    def trim_ms(self, keep_ms: int) -> None:
        """Drops the oldest audio so that keep_ms are left (latency built up at call start)."""
        with self.lock:
            keep = RATE * keep_ms // 1000 * FRAME_BYTES
            if len(self.buf) > keep:
                drop = len(self.buf) - keep
                drop -= drop % FRAME_BYTES
                del self.buf[:drop]
                self.dropped += drop

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


def find_capture_device(name: str, devices=None, hostapis=None):
    """The VB-CABLE recording end. A plain substring match is not enough: "Hi-Fi Cable
    Output" (the microphone cable) also contains "CABLE Output" (2026-11-20: Sonatina
    captured the wrong cable, with its own clock: pops). Microphone cables are skipped
    unless asked for, and names starting with `name` win; WASAPI first as before."""
    if devices is None:
        import sounddevice as sd
        devices, hostapis = sd.query_devices(), sd.query_hostapis()
    want = name.lower()
    avoid = [k.lower() for k in sonatina_hfp.MIC_DEVICES if k.lower() not in want]
    found = []
    for index, dev in enumerate(devices):
        n = dev["name"].lower()
        if dev["max_input_channels"] < 1 or want not in n or any(a in n for a in avoid):
            continue
        api = hostapis[dev["hostapi"]]["name"]
        found.append(((0 if "WASAPI" in api else 1, 0 if n.startswith(want) else 1, index),
                      index, dev, api))
    if not found:
        raise RuntimeError(f'no capture device matching "{name}" (is VB-CABLE installed?)')
    _rank, index, dev, api = min(found, key=lambda f: f[0])
    return index, dev, api


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
class EndpointVolume:
    """Follows the Windows volume and mute of the VB-CABLE playback endpoint.

    VB-CABLE ignores its own volume slider (the signal on "CABLE Output" is always
    full scale), so the gain is applied here, before encoding. When the headset has
    AVRCP absolute volume, `hardware` is set: the headset does the attenuation and the
    gain here only mutes. During a call (HFP) AVRCP volume does not reach the voice
    channel, so `in_call` brings the software gain back."""

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
        self.in_call = False                # HFP call: software gain even with `hardware`
        self.reading: tuple[bool, float, float] | None = None   # last (mute, slider, dB)
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
                # the Windows default output first, then "CABLE Input", then "CABLE In 16 Ch"
                rank = (0 if dev.GetId() == default_id else 1, 1 if is_16ch(name) else 0)
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
        self.reading = state
        mute, scalar, db = state
        self.scalar = scalar
        self._update_target()
        state = (mute, round(scalar * 100))
        if state != self.last:
            self.last = state
            log.info("volume %d%%%s", state[1], " (muted)" if mute else "")

    def _update_target(self) -> None:
        if self.reading is None:
            return
        mute, scalar, db = self.reading
        if mute or scalar <= 0.0:
            self.target = 0
        elif self.hardware and not self.in_call:
            self.target = self.UNITY
        else:
            self.target = min(self.UNITY, int(self.UNITY * 10 ** (db / 20)))

    def set_in_call(self, on: bool) -> None:
        """Call mode on/off: the gain follows Windows in software (no COM call here)."""
        self.in_call = on
        self._update_target()

    def apply(self, pcm: bytes, frames: int) -> bytes:
        g0, g1 = self.gain, self.target
        if g0 == self.UNITY and g1 == self.UNITY:
            return pcm
        buf = ctypes.create_string_buffer(pcm, len(pcm))
        self.lib.sont_pcm_gain(buf, frames, CHANNELS, g0, g1)
        self.gain = g1
        return buf.raw


def is_16ch(name: str) -> bool:
    """VB-CABLE's multichannel input: "CABLE In 16ch" in old versions, "CABLE In 16 Ch" now
    (2026-11-22: "16ch" did not match it and Sonatina made it the Windows output)."""
    return re.search(r"16\s*ch\b", name.lower()) is not None


def pick_cable_output(names: list[str], match: str = "VB-Audio Virtual Cable") -> int | None:
    """Index of the stereo VB-CABLE input among playback endpoint names (the 16-channel
    one only if nothing else is there)."""
    found = [(1 if is_16ch(n) else 0, i) for i, n in enumerate(names) if match.lower() in n.lower()]
    return min(found)[1] if found else None


def set_default_output(match: str = "VB-Audio Virtual Cable") -> str:
    """Makes the VB-CABLE playback endpoint the Windows default output (all roles)."""
    from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
    from pycaw.pycaw import AudioUtilities

    com_init()
    enumerator = AudioUtilities.GetDeviceEnumerator()
    endpoints = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
    devs = [endpoints.Item(i) for i in range(endpoints.GetCount())]
    names = [AudioUtilities.CreateDevice(d).FriendlyName or "" for d in devs]
    index = pick_cable_output(names, match)
    if index is None:
        raise RuntimeError(f'no playback endpoint matching "{match}" (is VB-CABLE installed?)')
    AudioUtilities.SetDefaultDevice(
        devs[index].GetId(), roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
    return names[index]


def fix_default_output(match: str = "VB-Audio Virtual Cable") -> str | None:
    """A microphone cable must never be the Windows default output: the music would go
    into the microphone instead of the headphones (installing Hi-Fi Cable does that).
    Returns the new default when it had to be changed."""
    if sys.platform != "win32":
        return None
    from pycaw.constants import EDataFlow, ERole
    from pycaw.pycaw import AudioUtilities

    com_init()
    enumerator = AudioUtilities.GetDeviceEnumerator()
    dev = enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender.value, ERole.eMultimedia.value)
    name = AudioUtilities.CreateDevice(dev).FriendlyName or ""
    if not any(k.lower() in name.lower() for k in sonatina_hfp.MIC_DEVICES):
        return None
    new = set_default_output(match)
    log.warning('Windows output was "%s" (the microphone cable): back to "%s"', name, new)
    return new


# ----------------------------------------------------------------------------- media pump
SILENCE = 32   # about -60 dBFS: paused players send zeros, quiet music stays above this


def not_silent(pcm: bytes) -> bool:
    samples = array.array("h")
    samples.frombytes(pcm)
    return bool(samples) and (max(samples) > SILENCE or min(samples) < -SILENCE)


class LinkBacklog:
    """Packets of our headset link still waiting in Bumble's queue (not yet handed to the
    controller, which holds at most a few). Bumble's queue has no limit: with the
    headphones out of range nothing leaves, seconds of audio pile up and arrive all at
    once when the radio recovers, which the headphones cannot take (silence, the two
    earbuds out of sync, a few seconds looping: 2026-11-21). The pump drops new audio
    instead while more than LIMIT_S of audio is waiting here (on top of what the
    controller holds), so what reaches the headphones stays a fraction of a second old."""

    LIMIT_S = 0.150
    GRACE_S = 3.0          # just after the start the headphones take audio slowly for a
    GRACE_LIMIT_S = 0.5    # moment (two 0.2 s drops at every connection, 2026-11-21)

    def __init__(self, channel) -> None:
        self.queue = None
        self.handle = None
        self.grace_until = time.monotonic() + self.GRACE_S
        try:
            connection = channel.connection
            self.handle = connection.handle
            self.queue = connection.device.host.get_data_packet_queue(connection.handle)
        except Exception as error:      # test channels, other transports: no limit
            log.debug("link backlog not available: %r", error)

    def waiting(self) -> int:
        packets = getattr(self.queue, "_packets", None)
        if not packets:
            return 0
        handle = self.handle
        return sum(1 for _packet, h in packets if h == handle)

    def full(self, packet_s: float, dropping: bool = False) -> bool:
        """packet_s: audio carried by one packet (LDAC 990: ~5 ms, aptX: ~19 ms). Once
        dropping, keeps dropping until half the limit is left (no on/off flicker)."""
        limit = self.GRACE_LIMIT_S if self.in_grace() else self.LIMIT_S
        if dropping:
            limit /= 2
        return self.queue is not None and self.waiting() * packet_s >= limit

    def in_grace(self) -> bool:
        return time.monotonic() < self.grace_until


class LdacAdapter:
    """Adaptive LDAC (like Android's "best effort"): when the radio cannot keep up (audio
    piling up for it, or dropped), one step down 990 -> 660 -> 330 kbit/s; after a while
    with a clean link, one step back up, never above the quality chosen by the user. A
    step up that fails soon makes the next try wait longer. Wi-Fi on 2.4 GHz next to the
    adapter made LDAC 990 lose ~3 s of audio a minute (2026-11-21)."""

    DOWN_QUEUED_S = 0.080       # audio waiting for the radio that means it is behind
    CLEAN_QUEUED_S = 0.030      # below this the link counts as clean
    DOWN_EVERY_S = 2.0          # at most one step down per 2 s (libldac moves over frames)
    UP_AFTER_S = (10.0, 30.0, 60.0, 120.0)   # clean time before a step up, per failed try
    FAILED_UP_S = 20.0          # a step down this soon after a step up: that try failed

    def __init__(self, encoder, now: float) -> None:
        self.encoder = encoder
        self.ceiling = encoder.eqmid            # 0 = 990 (best) .. 2 = 330
        self.clean_since = now
        self.last_down = -1e9
        self.last_up = -1e9
        self.failed_ups = 0
        self.steps = 0

    def update(self, now: float, queued_s: float, dropped: bool) -> None:
        enc = self.encoder
        if dropped or queued_s >= self.DOWN_QUEUED_S:
            self.clean_since = now
            if enc.eqmid < 2 and now - self.last_down >= self.DOWN_EVERY_S:
                if now - self.last_up < self.FAILED_UP_S:
                    self.failed_ups += 1
                self._step(enc.eqmid + 1, now, "radio not keeping up")
                self.last_down = now
            return
        if queued_s > self.CLEAN_QUEUED_S:
            self.clean_since = now
            return
        wait = self.UP_AFTER_S[min(self.failed_ups, len(self.UP_AFTER_S) - 1)]
        if enc.eqmid > self.ceiling and now - self.clean_since >= wait:
            self._step(enc.eqmid - 1, now, f"clean for {wait:.0f} s")
            self.last_up = now
            self.clean_since = now

    def _step(self, eqmid: int, now: float, why: str) -> None:
        old = self.encoder.kbps
        if self.encoder.set_eqmid(eqmid):
            self.steps += 1
            log.info("LDAC %d -> %d kbit/s (%s)", old, self.encoder.kbps, why)


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
        self.radio_dropped = 0    # packets not sent: the radio link was not keeping up
        self.radio_episodes = 0
        self.adaptive = True      # LDAC: follow the radio (LdacAdapter)
        self.adapter: LdacAdapter | None = None
        self.link: LinkBacklog | None = None     # packets waiting for the radio (sonatina_radio)
        self.packet_s = PUSH_SAMPLES / RATE      # audio carried by the last packet

    def queued_s(self) -> float:
        """Audio waiting for the radio in Bumble's queue, in seconds (0 when unknown)."""
        link = self.link
        if link is None or self.task is None:
            return 0.0
        return link.waiting() * self.packet_s

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
        link = LinkBacklog(channel)
        self.link = link
        dropping = 0                # packets dropped in the current slow-radio episode
        warned_at = -1e9
        self.adapter = (LdacAdapter(self.encoder, last) if link.queue is not None
                        and hasattr(self.encoder, "set_eqmid") else None)
        next_adapt = last + 0.25
        packet_s = PUSH_SAMPLES / RATE
        dropped_seen = self.radio_dropped
        while True:
            now = time.perf_counter()
            media += (now - last) * self.rate
            last = now
            if now >= next_servo:
                next_servo = now + 1.0
                self._servo()
            if self.adapter is not None and now >= next_adapt:
                next_adapt = now + 0.25
                if not self.adaptive:               # turned off: back to the chosen quality
                    self.encoder.set_eqmid(self.adapter.ceiling)
                elif not link.in_grace():
                    self.adapter.update(now, link.waiting() * packet_s, self.radio_dropped != dropped_seen)
                dropped_seen = self.radio_dropped
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
                    self.seq += 1               # advanced for dropped packets too: the
                    self.timestamp += samples   # headphones see a gap, not a time shift
                    packet_s = samples / RATE
                    self.packet_s = packet_s
                    if link.full(samples / RATE, dropping > 0):
                        if not dropping:
                            self.radio_episodes += 1
                            if now - warned_at > 30:
                                warned_at = now
                                log.warning("radio link too slow (headphones far away?): "
                                            "dropping audio instead of delaying it")
                        dropping += 1
                        self.radio_dropped += 1
                        continue
                    if dropping:
                        if dropping * samples / RATE >= 0.1:
                            log.info("radio link back: %.1f s of audio dropped",
                                     dropping * samples / RATE)
                        dropping = 0
                    try:
                        channel.write(header + payload)
                    except Exception as error:   # link dropped under us: stop quietly
                        log.debug("media channel closed: %r", error)
                        return
                    self.packets += 1
                done += PUSH_SAMPLES
            next_media = (done + PUSH_SAMPLES) / RATE - self.LEAD_S
            wait = (next_media - media) / self.rate
            await asyncio.sleep(max(0.0005, wait))
