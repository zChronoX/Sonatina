"""Headset microphone for Sonatina: HFP Audio Gateway over the same dongle.

Windows has no Bluetooth stack on our dongle, so the headset microphone reaches the apps
through a second virtual cable (Hi-Fi Cable, VB-CABLE A+B or the Steam microphone):

    headset mic --SCO--> Sonatina --> "Hi-Fi Cable Input"  ==>  "Hi-Fi Cable Output" = mic of the apps
    Windows audio (CABLE Output) --> Sonatina --SCO--> headset (during a call)

The service level connection (HFP over RFCOMM) stays up next to A2DP. When an app opens the
virtual microphone (a capture session on its recording endpoint), A2DP is suspended, an
eSCO link is opened (mSBC 16 kHz, or CVSD 8 kHz) and Windows audio goes to the headset over
it; when the app closes the microphone, A2DP comes back.

The SCO link is paced by the headset: every packet received is answered with a packet of
the same size, so no clock servo is needed on that side.

USB notes (found with tools/hfp_probe.py, 2026-11-20, Realtek RTL8761BUV):
- SCO over USB needs isochronous endpoints on an associated interface: libusb >= 1.0.27
  (third_party/libusb, 1.0.29).
- libusb 1.0.29 on WinUSB rewrites the iso descriptor lengths as Offset[i+1]-Offset[i] and
  WinUSB leaves later offsets at 0 (length -2401): usb1.iterISO then reads outside the
  buffer and Python dies ('_PyThreadState_Attach'). install_safe_iso() reads packet i at
  i * slot from the transfer's own buffer instead.
- Alternate setting 2 (17 bytes/ms) carries one 16-bit CVSD link; 1 is too narrow.
- At alternate 2 a 48-byte SCO packet (51 bytes with its header) fills exactly the 3 USB
  frames of audio it carries: the OUT stream must run without gaps. Bumble sends every OUT
  packet (commands, ACL, SCO) one transfer at a time, which leaves a gap after each one:
  ScoOutLane keeps several isochronous transfers in flight, apart from the ACL queue.
- Bumble reads SCO in 128-packet transfers (128 ms at full speed): tune_usb_transport()
  makes them 24 ms, so the microphone and the answers to it flow evenly.
- mSBC (transparent data) needs alternate setting 1: the controller then sends 24-byte
  packets (27 bytes = 3 frames of 9). At alternate 2 the RTL8761B framed them wrongly:
  30 % of the frames out of sync and nothing audible in the headset (2026-11-20). Linux
  btusb switches per call too (CVSD 16 bit -> 2, transparent -> 1, or 6 when it exists):
  UsbScoAlt does it while the transport stays open.
"""

from __future__ import annotations

import array
import asyncio
import collections
import ctypes
import dataclasses
import logging
import os
import sys
import struct
import threading
import time

log = logging.getLogger("sonatina.hfp")

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


# ----------------------------------------------------------------------------- USB
def bundled_libusb() -> str | None:
    """libusb 1.0.29: third_party/ in a checkout, next to Sonatina.exe when installed."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "third_party", "libusb", "libusb-1.0.dll")]
    if getattr(sys, "frozen", False):
        candidates.insert(0, os.path.join(os.path.dirname(sys.executable), "libusb-1.0.dll"))
        candidates.insert(1, os.path.join(getattr(sys, "_MEIPASS", ""), "libusb-1.0.dll"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def use_bundled_libusb() -> str | None:
    """Bumble loads libusb from libusb_package (1.0.26: no isochronous on associated
    interfaces). Point it at ours. Windows only: elsewhere the system libusb is fine."""
    if sys.platform != "win32":
        return None
    path = bundled_libusb()
    if path is None:
        return None
    try:
        import libusb_package
    except ImportError:
        return None
    libusb_package.get_library_path = lambda: path
    return path


_SAFE_ISO = False
ISO_TRACE: list | None = None     # (time, packet index, status, bytes) of SCO IN packets while dumping


def install_safe_iso() -> None:
    """Replaces usb1.USBTransfer.iterISO (see the module notes). Idempotent."""
    global _SAFE_ISO
    if _SAFE_ISO:
        return
    try:
        import usb1
        from usb1 import libusb1
    except ImportError:
        return
    warned = {"n": 0}

    def iterISO(self):
        transfer_p = self._USBTransfer__transfer
        transfer = transfer_p.contents
        data = self.getBuffer()
        size = len(data)
        count = transfer.num_iso_packets
        if count <= 0 or count > 1024 or size % count:
            return
        slot = size // count
        for i, d in enumerate(libusb1.get_iso_packet_list(transfer_p)):
            actual = d.actual_length
            if actual > slot:
                if warned["n"] < 3:
                    warned["n"] += 1
                    log.warning("iso packet %d: %d bytes > slot %d (clamped)", i, actual, slot)
                actual = slot
            start = i * slot
            trace = ISO_TRACE
            if trace is not None:
                trace.append((time.monotonic(), i, d.status, d.actual_length, slot))
            yield d.status, bytearray(data[start:start + actual])

    usb1.USBTransfer.iterISO = iterISO
    _SAFE_ISO = True


def tune_usb_transport() -> None:
    """Shorter SCO IN transfers (see the module notes). Multiple of 8: WinUSB wants whole
    frames even on high-speed adapters."""
    try:
        from bumble.transport import usb as bumble_usb
    except ImportError:
        return
    bumble_usb.MAX_SCO_IN_PACKETS = 24
    bumble_usb.NUMBER_OF_SCO_IN_TRANSFERS = 4


class ScoOutLane:
    """SCO packets to the controller on their own isochronous transfers, several in flight
    (continuous stream), while ACL and commands keep Bumble's queue."""

    IN_FLIGHT = 8             # 24 ms queued in WinUSB: one IN burst; 4 left gaps (drops, 2026-11-20)
    MAX_PENDING = 24          # ~72 ms of 48-byte packets: older ones are dropped

    @classmethod
    def install(cls, sink):
        if getattr(sink, "isochronous_out", None) is None or getattr(sink, "device", None) is None:
            return None
        return cls(sink)

    def __init__(self, sink) -> None:
        from bumble import hci

        self.sink = sink
        self.loop = sink.loop
        self.address = sink.isochronous_out.getAddress()
        self.mps = sink.isochronous_out.getMaxPacketSize()
        iso_packets = max(1, 1024 // self.mps)
        self.transfers = [sink.device.getTransfer(iso_packets=iso_packets) for _ in range(self.IN_FLIGHT)]
        self.free = list(self.transfers)
        self.pending: collections.deque = collections.deque()
        self.sent = 0
        self.done = 0
        self.dropped = 0
        self.errors = 0
        self.failed = 0
        self.closed = False
        self.sco_type = hci.HCI_SYNCHRONOUS_DATA_PACKET
        try:
            import usb1
            self.completed = usb1.TRANSFER_COMPLETED
        except ImportError:
            self.completed = 0
        self.on_idle = None             # () -> HCI SCO payload or None: fills gaps (ScoAudio)
        self.original_on_packet = sink.on_packet
        self.original_terminate = sink.terminate
        sink.on_packet = self.on_packet
        sink.terminate = self.terminate
        log.info("SCO out: %d transfers in flight, %d-byte iso packets", self.IN_FLIGHT, self.mps)

    def on_packet(self, packet) -> None:
        if self.closed or not packet or packet[0] != self.sco_type:
            self.original_on_packet(packet)
            return
        payload = bytes(packet[1:])
        if self.free:
            self._submit(self.free.pop(), payload)
            return
        self.pending.append(payload)
        while len(self.pending) > self.MAX_PENDING:
            self.pending.popleft()
            self.dropped += 1

    def _submit(self, transfer, payload: bytes) -> None:
        mps = self.mps
        n = (len(payload) + mps - 1) // mps
        lengths = [mps] * (n - 1) + [len(payload) - mps * (n - 1)]
        try:
            transfer.setIsochronous(self.address, payload, callback=self._done,
                                    iso_transfer_length_list=lengths)
            transfer.submit()
            self.sent += 1
        except Exception as error:
            self.errors += 1
            if self.errors <= 3:
                log.warning("SCO out: %r", error)
            self.free.append(transfer)

    def _done(self, transfer) -> None:      # libusb event thread
        try:
            self.loop.call_soon_threadsafe(self._release, transfer)
        except RuntimeError:
            pass                            # loop closed: shutting down

    def _release(self, transfer) -> None:
        if self.closed:
            return
        self.done += 1
        status = transfer.getStatus()
        if status != self.completed:
            self.failed += 1
            if self.failed <= 3:
                log.warning("SCO out transfer status %s", status)
        if self.pending:
            self._submit(transfer, self.pending.popleft())
            return
        payload = None
        if self.on_idle is not None:
            try:
                payload = self.on_idle()
            except Exception as error:
                log.debug("SCO out idle: %r", error)
        if payload:
            self._submit(transfer, payload)
        else:
            self.free.append(transfer)

    def retarget(self, endpoint) -> None:
        self.address = endpoint.getAddress()
        self.mps = endpoint.getMaxPacketSize()

    def backlog(self) -> int:
        return len(self.pending)

    def stats(self) -> str:
        return (f"usb out {self.sent} done {self.done} failed {self.failed} pending {len(self.pending)}"
                f" dropped {self.dropped} errors {self.errors}")

    async def terminate(self) -> None:
        self.closed = True
        self.pending.clear()
        for transfer in self.transfers:
            try:
                if transfer.isSubmitted():
                    transfer.cancel()
            except Exception:
                pass
        await asyncio.sleep(0.05)           # let libusb deliver the cancellations
        await self.original_terminate()


class UsbScoAlt:
    """Changes the SCO alternate setting of an open Bumble USB transport.

    The isochronous IN transfers are parked (their callback is swapped so they are not
    submitted again; no cancel, which Bumble would take for an unplugged adapter), the
    interface switches, and they are resubmitted with the new packet size."""

    def __init__(self, transport) -> None:
        self.transport = transport
        self.source = transport.source
        self.sink = transport.sink
        self.interface = transport.sco_interface.getNumber()
        self.alt = transport.sco_interface.getAlternateSetting()
        self.endpoints: dict[int, tuple] = {}     # alt -> (iso in, iso out)
        for configuration in transport.device.getDevice():
            for interface in configuration:
                for setting in interface:
                    if setting.getNumber() != self.interface:
                        continue
                    iso_in = iso_out = None
                    for endpoint in setting:
                        if endpoint.getAttributes() & 0x03 != 0x01:
                            continue
                        if endpoint.getAddress() & 0x80:
                            iso_in = endpoint
                        else:
                            iso_out = endpoint
                    if iso_in is not None and iso_out is not None and iso_in.getMaxPacketSize():
                        self.endpoints[setting.getAlternateSetting()] = (iso_in, iso_out)
        log.info("SCO alternate settings: %s", ", ".join(
            f"{a}: {i.getMaxPacketSize()} B" for a, (i, _o) in sorted(self.endpoints.items())))

    @classmethod
    def install(cls, transport):
        if getattr(transport, "sco_interface", None) is None or not hasattr(transport, "source"):
            return None
        try:
            return cls(transport)
        except Exception as error:
            log.warning("SCO alternate settings unavailable (%r)", error)
            return None

    async def switch(self, alt: int, lane: "ScoOutLane | None" = None) -> bool:
        if alt == self.alt:
            return True
        if alt not in self.endpoints:
            log.warning("SCO alternate setting %d does not exist", alt)
            return False
        from bumble import hci
        from bumble.transport import usb as bumble_usb

        source = self.source
        transfers = list(source.isochronous_in_transfers)
        original = source.transfer_callback

        def park(_transfer) -> None:      # libusb event thread: just do not resubmit
            pass

        for transfer in transfers:
            transfer.setCallback(park)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.5
        while any(t.isSubmitted() for t in transfers) or (lane is not None and len(lane.free) < len(lane.transfers)):
            if loop.time() > deadline:
                log.warning("SCO alternate setting: transfers still busy, not switching")
                for transfer in transfers:
                    transfer.setCallback(original)
                return False
            await asyncio.sleep(0.005)
        if lane is not None:
            lane.pending.clear()
        try:
            self.transport.device.setInterfaceAltSetting(self.interface, alt)
        except Exception as error:
            log.warning("SCO alternate setting %d: %r", alt, error)
            alt = self.alt          # resubmit on the old one
        iso_in, iso_out = self.endpoints[alt]
        size = iso_in.getMaxPacketSize()
        source.isochronous_in = iso_in
        for transfer in transfers:
            transfer.setIsochronous(iso_in.getAddress(), bumble_usb.MAX_SCO_IN_PACKETS * size,
                                    callback=original, user_data=hci.HCI_SYNCHRONOUS_DATA_PACKET)
            source.done[transfer] = asyncio.Event()
            transfer.submit()
        self.sink.isochronous_out = iso_out
        if lane is not None:
            lane.retarget(iso_out)
        changed = alt != self.alt
        self.alt = alt
        if changed:
            log.info("SCO alternate setting %d (%d-byte packets)", alt, size)
        return changed


def transport_spec(spec: str, alt: int) -> str:
    """usb:VID:PID -> usb:VID:PID+sco=<alt> (SCO endpoints on)."""
    if not spec.lower().startswith("usb") or "+sco=" in spec or alt <= 0:
        return spec
    return f"{spec}+sco={alt}"


# ----------------------------------------------------------------------------- HCI
_LEGACY_SETUP = None


def legacy_setup_command():
    """HCI Setup Synchronous Connection (7.1.26): not in Bumble, and the Realtek
    RTL8761B does not implement the Enhanced variant."""
    global _LEGACY_SETUP
    if _LEGACY_SETUP is not None:
        return _LEGACY_SETUP
    from bumble.hci import HCI_AsyncCommand, HCI_Command, metadata

    @HCI_Command.command
    @dataclasses.dataclass
    class HCI_Setup_Synchronous_Connection_Command(HCI_AsyncCommand):
        connection_handle: int = dataclasses.field(metadata=metadata(2))
        transmit_bandwidth: int = dataclasses.field(metadata=metadata(4))
        receive_bandwidth: int = dataclasses.field(metadata=metadata(4))
        max_latency: int = dataclasses.field(metadata=metadata(2))
        voice_setting: int = dataclasses.field(metadata=metadata(2))
        retransmission_effort: int = dataclasses.field(metadata=metadata(1))
        packet_type: int = dataclasses.field(metadata=metadata(2))

    _LEGACY_SETUP = HCI_Setup_Synchronous_Connection_Command
    return _LEGACY_SETUP


def msbc_transparent_parameters():
    """Enhanced Setup parameters for mSBC coded by the host (transparent data path),
    from the T2 settings. Bumble's own MSBC entries have the controller do the coding."""
    from bumble import hfp
    from bumble.hci import CodecID, CodingFormat

    base = hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_MSBC_T2]
    return dataclasses.replace(
        base,
        transmit_coding_format=CodingFormat(CodecID.TRANSPARENT),
        receive_coding_format=CodingFormat(CodecID.TRANSPARENT),
        input_coding_format=CodingFormat(CodecID.TRANSPARENT),
        output_coding_format=CodingFormat(CodecID.TRANSPARENT),
        input_coded_data_size=8,
        output_coded_data_size=8,
        input_bandwidth=8000,
        output_bandwidth=8000,
    )


# ----------------------------------------------------------------------------- HFP AG
def ag_configuration(msbc: bool):
    from bumble import hfp

    return hfp.AgConfiguration(
        supported_ag_features=[hfp.AgFeature.CODEC_NEGOTIATION,
                               hfp.AgFeature.ESCO_S4_SETTINGS_SUPPORTED,
                               hfp.AgFeature.ENHANCED_CALL_STATUS],
        supported_ag_indicators=[hfp.AgIndicatorState.call(),
                                 hfp.AgIndicatorState.callsetup(),
                                 hfp.AgIndicatorState.callheld(),
                                 hfp.AgIndicatorState.service(),
                                 hfp.AgIndicatorState.signal(),
                                 hfp.AgIndicatorState.roam(),
                                 hfp.AgIndicatorState.battchg()],
        supported_hf_indicators=[],
        supported_ag_call_hold_operations=[],
        supported_audio_codecs=([hfp.AudioCodec.CVSD, hfp.AudioCodec.MSBC] if msbc
                                else [hfp.AudioCodec.CVSD]),
    )


def make_ag_class():
    from bumble import hfp

    class SonatinaAg(hfp.AgProtocol):
        """Bumble's AG plus the commands real headsets send during the SLC."""

        def _on_nrec(self, *_args) -> None:          # echo cancelling off: nothing to do
            self.send_ok()

        def _on_btrh_read(self) -> None:             # response and hold: no call held
            self.send_ok()

        def _on_cops(self, *_args) -> None:          # operator name format
            self.send_ok()

        def _on_cops_read(self) -> None:
            self.send_response('+COPS: 0,0,"Sonatina"')
            self.send_ok()

        def _on_cnum(self) -> None:                  # subscriber number: none
            self.send_ok()

        def _on_xapl(self, *_args) -> None:          # Apple accessory handshake
            self.send_response("+XAPL=iPhone,0")
            self.send_ok()

        def _on_iphoneaccev(self, *_args) -> None:   # Apple battery report
            self.send_ok()

        def _on_bldn(self) -> None:                  # redial: we place no calls
            self.send_error()

    return SonatinaAg


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
        global ISO_TRACE
        self.codec = codec
        self.seconds = seconds
        self.t0 = time.monotonic()
        self.lines: list[str] = []
        self.done = False
        ISO_TRACE = []

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
        global ISO_TRACE
        if self.done:
            return
        self.done = True
        trace, ISO_TRACE = ISO_TRACE or [], None
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

    rx: SCO payload -> (mSBC frames ->) 8/16 kHz PCM -> 48 kHz stereo -> gain -> mic_sink
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

    def __init__(self, lib, codec: str, ring, volume, mic_sink, mic_gain_db: float, send,
                 lane=None) -> None:
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
        self.up = Resampler(lib, self.rate, RATE, 1, 2)
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
        out = self.up.process(pcm)
        if not out:
            return
        samples = array.array("h")
        samples.frombytes(out)
        level = max(max(samples), -min(samples))
        target = self.gain_q16
        if level * target > self.LIMIT << 16:         # would clip: just enough gain, now
            target = max(self.gain_q16 // 4, (self.LIMIT << 16) // level)
            self.limited += 1
        if target > self.cur_gain:                     # release: about 6 dB in 0.5 s
            target = min(target, self.cur_gain + max(1, self.cur_gain // 150))
        if (target != 65536 or self.cur_gain != 65536) and self.lib_has_gain():
            buf = ctypes.create_string_buffer(out, len(out))
            self.lib.sont_pcm_gain(buf, len(out) // 4, 2, self.cur_gain, target)
            out = buf.raw
        self.cur_gain = target
        level = level * target >> 16
        if level > self.mic_peak:
            self.mic_peak = level
        self.level = max(level / 32767, self.level * 0.9)
        self.mic_sink(out)

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
                + f" silent blocks {self.silent_pulls}")

    def close(self) -> None:
        self.closed = True
        if self.lane is not None and self.lane.on_idle == self._idle_packet:
            self.lane.on_idle = None
        if self.dump is not None:
            self.dump.write()
        self.up.close()
        self.down.close()
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


# ----------------------------------------------------------------------------- gateway
class HfpGateway:
    """HFP AG for the connected headset, and the call mode switching."""

    WATCH_S = 0.5
    ENTER_AFTER_S = 0.4
    LEAVE_AFTER_S = 2.0

    def __init__(self, host) -> None:
        self.host = host
        self.s = host.s
        self.lib = host.lib
        self.device = None
        self.channel = None
        self.ag = None
        self.connection = None
        self.ag_ready: asyncio.Future | None = None
        self.headset_mux = False
        self.sco = None
        self.audio: ScoAudio | None = None
        self.codec: str | None = None
        self.in_call = False
        self.busy = asyncio.Lock()
        self.dismissed = False          # hung up from the headset: wait for the app to close
        self.mic_key: str | None = None
        self.mic_out: MicOutput | None = None
        self.watcher: MicWatcher | None = None
        self.mic_since: float | None = None
        self.idle_since: float | None = None
        self.sco_failures = 0
        self.slc_task: asyncio.Task | None = None
        self.last_error: str | None = None
        self.lane: ScoOutLane | None = None   # set by amain on USB adapters
        self.usb_alt: UsbScoAlt | None = None
        self._msbc_window = None
        self.make_watcher = MicWatcher        # tests replace these
        self.make_mic_out = MicOutput

    # -- setup -----------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.s.hfp_mode in ("auto", "on")

    def can_msbc(self) -> bool:
        return bool(self.s.hfp_msbc) and getattr(self.host.lib, "has_msbc", False)

    def attach(self, device) -> None:
        """RFCOMM server + AG SDP record (before power on)."""
        if not self.enabled:
            return
        from bumble import hfp, rfcomm

        self.device = device
        server = rfcomm.Server(device)
        self.channel = server.listen(self._on_dlc)
        server.on("start", self._on_mux)
        device.sdp_service_records[AG_RECORD_HANDLE] = hfp.make_ag_sdp_records(
            AG_RECORD_HANDLE, self.channel, ag_configuration(self.can_msbc()))
        device.on("sco_connection", self._on_sco_connection)
        device.on("sco_connection_failure", self._on_sco_failure)
        self._sco_waiter: asyncio.Future | None = None

    def _on_mux(self, multiplexer) -> None:
        connection = multiplexer.l2cap_channel.connection
        if connection is self.connection:
            self.headset_mux = True
            log.debug("headset opened an RFCOMM session")

    def _on_dlc(self, dlc) -> None:
        connection = dlc.multiplexer.l2cap_channel.connection
        if connection is not self.connection or self.ag is not None:
            log.info("HFP: unexpected RFCOMM channel, ignored")
            return
        log.info("HFP: the headset opened the hands-free channel")
        self._bind(dlc)

    def _bind(self, dlc) -> None:
        ag = make_ag_class()(dlc, ag_configuration(self.can_msbc()))
        ag.on("slc_complete", lambda: self._slc_complete(ag))
        ag.on("hang_up", self._on_hang_up)
        ag.on("answer", self._on_answer)
        ag.on("speaker_volume", self._on_speaker_volume)
        self.ag = ag

    def _slc_complete(self, ag) -> None:
        if self.ag_ready is not None and not self.ag_ready.done():
            self.ag_ready.set_result(ag)

    # -- per connection --------------------------------------------------------
    def on_connected(self, connection) -> None:
        if not self.enabled or self.device is None:
            return
        self.connection = connection
        self.ag = None
        self.headset_mux = False
        self.ag_ready = asyncio.get_running_loop().create_future()

    def start_slc(self) -> None:
        """After A2DP is streaming (starting both at once confuses some headsets)."""
        if not self.enabled or self.connection is None:
            return
        if self.slc_task is None or self.slc_task.done():
            self.slc_task = asyncio.get_running_loop().create_task(self._slc(self.connection))

    def on_disconnected(self) -> None:
        if self.slc_task is not None:
            self.slc_task.cancel()
            self.slc_task = None
        self._stop_audio()
        self.sco = None
        self.ag = None
        self.connection = None
        self.in_call = False
        self.codec = None
        if self.ag_ready is not None and not self.ag_ready.done():
            self.ag_ready.cancel()

    @property
    def ready(self) -> bool:
        return self.ag_ready is not None and self.ag_ready.done() and not self.ag_ready.cancelled()

    async def _slc(self, connection) -> None:
        from bumble import hfp, rfcomm

        delays = [6.0, 4.0, 8.0, 15.0]
        for attempt, wait in enumerate(delays):
            if connection is not self.connection:
                return
            try:
                await asyncio.wait_for(asyncio.shield(self.ag_ready), wait)
                break
            except asyncio.TimeoutError:
                pass
            if self.ready:
                break
            if self.headset_mux or self.ag is not None:
                # only one RFCOMM session per link: the headset owns it, give it time
                try:
                    await asyncio.wait_for(asyncio.shield(self.ag_ready), 15.0)
                    break
                except asyncio.TimeoutError:
                    log.info("HFP: the headset opened a session but no hands-free channel")
                    continue
            try:
                record = await asyncio.wait_for(hfp.find_hf_sdp_record(connection), 10.0)
                if not record:
                    log.info("HFP: this headset has no hands-free service (no microphone)")
                    return
                channel, version, _features = record
                log.info("HFP: opening the hands-free channel %d (HF version 0x%04X, attempt %d)",
                         channel, version, attempt + 1)
                mux = await asyncio.wait_for(rfcomm.Client(connection).start(), 10.0)
                dlc = await asyncio.wait_for(mux.open_dlc(channel), 10.0)
                if self.ag is None:
                    self._bind(dlc)
                await asyncio.wait_for(asyncio.shield(self.ag_ready), 10.0)
                break
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.info("HFP: attempt %d failed (%s)", attempt + 1, type(error).__name__)
        else:
            log.warning("HFP: no hands-free connection, microphone not available")
            return
        ag = self.ag_ready.result()
        codecs = [c.name for c in ag.supported_audio_codecs] or ["CVSD"]
        log.info("HFP: ready (headset codecs %s)", ", ".join(codecs))
        self.host._changed_state()

    # -- headset buttons -------------------------------------------------------
    def _on_hang_up(self) -> None:
        if self.in_call:
            log.info("HFP: hung up from the headset: back to music until the app closes the microphone")
            self.dismissed = True
            asyncio.get_running_loop().create_task(self.leave_call())

    def _on_speaker_volume(self, level: int) -> None:
        """Volume buttons of the headset during a call (AT+VGS, 0..15): moves Windows."""
        volume = self.host.volume
        if not self.in_call or volume is None:
            return
        level = max(0, min(15, int(level)))
        log.info("HFP: headset volume %d/15", level)
        volume.set_scalar(level / 15, unmute=level > 0)

    def _on_answer(self) -> None:
        if not self.in_call and self.dismissed:
            log.info("HFP: answered from the headset: call mode again")
            self.dismissed = False

    # -- call mode -------------------------------------------------------------
    def choose_codec(self) -> str:
        from bumble import hfp
        ag = self.ag
        msbc_ok = (self.can_msbc() and ag is not None
                   and hfp.AudioCodec.MSBC in ag.supported_audio_codecs
                   and ag.supports_hf_feature(hfp.HfFeature.CODEC_NEGOTIATION)
                   and not self.host.s.headsets.get(self.host.peer or "", {}).get("msbc_broken"))
        return "msbc" if msbc_ok else "cvsd"

    async def enter_call(self) -> bool:
        async with self.busy:
            if self.in_call or not self.ready or self.connection is None:
                return self.in_call
            codec = self.choose_codec()
            try:
                log.info("call mode: starting (%s)", codec)
                await self.host.suspend_media()
                if self.usb_alt is not None:
                    alt = int(self.s.sco_alt_msbc if codec == "msbc" else self.s.sco_alt)
                    await self.usb_alt.switch(alt, self.lane)
                    if self.usb_alt.alt != alt:
                        raise RuntimeError(f"cannot use USB alternate setting {alt}")
                log.debug("call mode: A2DP suspended")
                await self._negotiate(codec)
                log.debug("call mode: codec negotiated")
                link = await self._open_sco(codec)
            except Exception as error:
                self.last_error = repr(error)
                log.warning("HFP: call mode failed (%r)", error)
                self.sco_failures += 1
                await self.host.resume_media()
                return False
            self.sco = link
            self.codec = codec
            try:
                self._start_audio(link, codec)
            except Exception as error:
                log.exception("HFP: audio start failed: %r", error)
                self.sco = None
                self._stop_audio()
                try:
                    await link.disconnect()
                except Exception:
                    pass
                self.sco_failures += 1
                await self.host.resume_media()
                return False
            self.in_call = True
            self.sco_failures = 0
            log.info("call mode: %s (%d kHz), microphone -> %s", "mSBC" if codec == "msbc" else "CVSD",
                     16 if codec == "msbc" else 8, self.mic_out.name if self.mic_out else "-")
            self.host._changed_state()
            return True

    async def leave_call(self) -> None:
        async with self.busy:
            if not self.in_call:
                return
            self.in_call = False
            if self.audio is not None:
                log.info("call ended: %s%s", self.audio.stats(),
                         f", {self.lane.stats()}" if self.lane is not None else "")
            self._stop_audio()
            link, self.sco = self.sco, None
            if link is not None:
                try:
                    await asyncio.wait_for(link.disconnect(), 5.0)
                except Exception as error:
                    log.debug("sco disconnect: %r", error)
            await asyncio.sleep(0.3)
            await self.host.resume_media()
            self.host._changed_state()

    async def _negotiate(self, codec: str) -> None:
        from bumble import hfp
        ag = self.ag
        if ag.supports_hf_feature(hfp.HfFeature.CODEC_NEGOTIATION) and ag.supports_ag_feature(
                hfp.AgFeature.CODEC_NEGOTIATION):
            target = hfp.AudioCodec.MSBC if codec == "msbc" else hfp.AudioCodec.CVSD
            await asyncio.wait_for(ag.negotiate_codec(target), 5.0)

    def _on_sco_connection(self, link) -> None:
        waiter = getattr(self, "_sco_waiter", None)
        if waiter is not None and not waiter.done():
            waiter.set_result(link)
        else:
            log.info("SCO link not requested by us: closing it")
            asyncio.get_running_loop().create_task(link.disconnect())

    def _on_sco_failure(self, *error) -> None:
        waiter = getattr(self, "_sco_waiter", None)
        if waiter is not None and not waiter.done():
            waiter.set_exception(RuntimeError(f"SCO setup failed: {error}"))

    async def _open_sco(self, codec: str):
        from bumble import hci, hfp

        device, connection = self.device, self.connection
        enhanced = device.host.supports_command(hci.HCI_ENHANCED_SETUP_SYNCHRONOUS_CONNECTION_COMMAND)
        legacy = device.host.supports_command(hci.HCI_SETUP_SYNCHRONOUS_CONNECTION_COMMAND)
        loop = asyncio.get_running_loop()
        if enhanced or not legacy:   # neither advertised: try both (virtual controllers)
            params = (msbc_transparent_parameters() if codec == "msbc"
                      else hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_CVSD_S4])
            self._sco_waiter = loop.create_future()
            try:
                await device.send_command(hci.HCI_Enhanced_Setup_Synchronous_Connection_Command(
                    connection_handle=connection.handle, **params.asdict()))
                link = await asyncio.wait_for(self._sco_waiter, 10.0)
                log.info("eSCO up (enhanced setup): handle 0x%04X", link.handle)
                return link
            except Exception as error:
                if enhanced:
                    raise
                log.info("enhanced SCO setup failed (%r): trying the classic command", error)
        voice = VOICE_TRANSPARENT if codec == "msbc" else VOICE_CVSD
        setup = legacy_setup_command()
        last = None
        for name, latency, effort, packets in (MSBC_SETTINGS if codec == "msbc" else CVSD_SETTINGS):
            self._sco_waiter = loop.create_future()
            try:
                await device.send_command(setup(
                    connection_handle=connection.handle, transmit_bandwidth=8000,
                    receive_bandwidth=8000, max_latency=latency, voice_setting=voice,
                    retransmission_effort=effort, packet_type=packets))
                link = await asyncio.wait_for(self._sco_waiter, 6.0)
                log.info("SCO %s up: handle 0x%04X, rx %d tx %d", name, link.handle,
                         link.rx_packet_length, link.tx_packet_length)
                return link
            except Exception as error:
                last = error
                log.info("SCO %s failed: %r", name, error)
        raise RuntimeError(f"no synchronous link ({last!r})")

    def _start_audio(self, link, codec: str) -> None:
        if self.mic_out is None and self.mic_key:
            try:
                out = self.make_mic_out(self.mic_key)
                out.start()
                self.mic_out = out
            except Exception as error:
                log.warning("microphone output not available (%r)", error)
        sink = self.mic_out.write if self.mic_out is not None else (lambda _pcm: None)
        host = self.device.host
        handle = link.handle
        self.host.ring.set_prefill(30)
        self.host.ring.reset()
        if self.host.volume is not None:
            self.host.volume.set_in_call(True)    # AVRCP volume does not reach the voice channel
        self.audio = ScoAudio(self.lib.lib, codec, self.host.ring, self.host.volume, sink,
                              float(self.s.mic_gain_db),
                              lambda sdu: host.send_sco_sdu(connection_handle=handle, sdu=sdu),
                              lane=self.lane)
        seconds = ScoDump.seconds_from_env()
        if seconds > 0:
            self.audio.dump = ScoDump(codec, seconds)
            log.info("SCO dump on for %.0f s", seconds)
        link.sink = self.audio.on_packet
        link.on("disconnection", lambda *_a: self._on_sco_closed(link))

    def _on_sco_closed(self, link) -> None:
        if self.sco is link and self.in_call:
            log.info("SCO link closed by the headset")
            self.sco = None
            asyncio.get_running_loop().create_task(self.leave_call())

    async def shutdown(self) -> None:
        """Before the adapter closes: end a call without resuming A2DP (closing with an
        open SCO link hung the USB close, 2026-11-20)."""
        if not self.in_call:
            return
        self.in_call = False
        self._stop_audio()
        link, self.sco = self.sco, None
        if link is not None:
            try:
                await asyncio.wait_for(link.disconnect(), 2.0)
            except BaseException as error:
                log.debug("sco disconnect at shutdown: %r", error)

    def _stop_audio(self) -> None:
        if self.audio is not None:
            self.audio.close()
            self.audio = None
        if self.host.volume is not None:
            self.host.volume.set_in_call(False)
        if self.mic_out is not None:
            self.mic_out.stop()
            self.mic_out = None

    # -- the loop deciding call mode ------------------------------------------
    async def run(self) -> None:
        """Watches the virtual microphone and switches A2DP <-> call mode."""
        if not self.enabled:
            return
        loop = asyncio.get_running_loop()
        next_lookup = 0.0
        msbc_check_at = None
        previous = loop.time()
        while True:
            await asyncio.sleep(self.WATCH_S)
            now = loop.time()
            # how late this turn is, including the work of the last one (COM calls...)
            self._lag_ms = max(getattr(self, "_lag_ms", 0.0), (now - previous - self.WATCH_S) * 1000)
            previous = now
            if sys.platform == "win32" and now >= getattr(self, "_next_output_check", 0.0):
                self._next_output_check = now + 30.0
                try:
                    import sonatina_host
                    sonatina_host.fix_default_output()
                except Exception as error:
                    log.debug("default output check: %r", error)
            if self.mic_key is None and now >= next_lookup:
                next_lookup = now + 10.0
                self.mic_key = await loop.run_in_executor(None, find_mic_cable, self.s.mic_device) \
                    if sys.platform == "win32" else (self.s.mic_device or None)
                if self.mic_key:
                    log.info("virtual microphone: %s", self.mic_key)
                    self.watcher = self.make_watcher(self.mic_key)
                elif not getattr(self, "_warned_cable", False):
                    self._warned_cable = True
                    log.warning("no virtual microphone (install Hi-Fi Cable): the headset "
                                "microphone cannot reach the apps")
            try:
                wanted = self._wanted(now)
                if wanted and not self.in_call and self.ready and self.sco_failures < 3:
                    if await self.enter_call() and self.codec == "msbc":
                        msbc_check_at = now + 1.5          # warm-up: the link starts with zeros
                        self._msbc_window = None
                elif not wanted and self.in_call:
                    await self.leave_call()
                if self.in_call and self.audio is not None and now >= getattr(self, "_next_stats", 0.0):
                    first = now - self.audio.started < 60
                    if getattr(self, "_stats_shown", False):
                        log.info("call: %s; %s, %s, windows ring %d ms underruns %d dropped %d smp,"
                                 " loop lag max %.0f ms", self.audio.window_stats(), self.audio.stats(),
                                 self.lane.stats() if self.lane else "-", self.host.ring.level_ms(),
                                 self.host.ring.underruns, self.host.ring.dropped // 4,
                                 self._take_lag_ms())
                    else:
                        self.audio.window_stats()          # start the first window now
                        self._take_lag_ms()
                    self._stats_shown = True
                    self._next_stats = now + (10.0 if first else 30.0)
                elif not self.in_call:
                    self._stats_shown = False
                    self._next_stats = 0.0
                if msbc_check_at is not None and now >= msbc_check_at:
                    msbc_check_at = now + 2.0 if await self._check_msbc(now) else None
            except asyncio.CancelledError:
                raise
            except Exception as error:      # keep watching whatever happens
                log.exception("HFP: %r", error)
                await asyncio.sleep(2.0)

    def _take_lag_ms(self) -> float:
        """Worst event loop delay seen since the last call (a stalled loop loses SCO data)."""
        lag, self._lag_ms = getattr(self, "_lag_ms", 0.0), 0.0
        return lag

    def _wanted(self, now: float) -> bool:
        if self.s.hfp_mode == "on":
            return not self.dismissed
        if self.watcher is None:
            return False
        apps = self.watcher.poll()
        if apps is None:
            return self.in_call        # cannot tell: keep the current mode
        if apps:
            self.idle_since = None
            if self.mic_since is None:
                self.mic_since = now
            return not self.dismissed and (self.in_call or now - self.mic_since >= self.ENTER_AFTER_S)
        self.mic_since = None
        self.dismissed = False
        if not self.in_call:
            return False
        if self.idle_since is None:
            self.idle_since = now
        return now - self.idle_since < self.LEAVE_AFTER_S

    async def _check_msbc(self, now: float) -> bool:
        """mSBC frames that do not decode: this adapter/headset pair needs CVSD.
        Checked over 2 s windows during the first 15 s of a call. Returns True to keep
        checking."""
        audio = self.audio
        if audio is None or audio.codec != "msbc" or not self.in_call:
            return False
        if self._msbc_window is None:           # end of the warm-up: start counting here
            self._msbc_window = (audio.frames_ok, audio.frames_lost, now)
            return True
        ok0, lost0, started = self._msbc_window
        ok, lost = audio.frames_ok - ok0, audio.frames_lost - lost0
        self._msbc_window = (audio.frames_ok, audio.frames_lost, started)
        total = ok + lost
        expected = 2.0 / 0.0075 * 0.5          # at least half of the frames of 2 s arrived
        bad = (total >= expected and ok / total < 0.85) or (audio.rx_packets > 300 and audio.frames_ok == 0)
        if bad and audio.dump is not None:
            log.warning("mSBC: %d of %d frames good (dump running: no fallback)", ok, total)
            bad = False
        if bad:
            log.warning("mSBC does not work here (%d of %d frames good, %s): using CVSD for this "
                        "headset", ok, total, audio.stats())
            if self.host.peer:
                self.host.s.headsets.setdefault(self.host.peer, {})["msbc_broken"] = True
                self.host._changed()
            await self.leave_call()
            return False
        if now - started > 15.0:
            log.info("mSBC ok: %s", audio.stats())
            return False
        return True

    # -- tray ------------------------------------------------------------------
    def summary(self) -> str | None:
        if not self.in_call:
            return None
        codec = "mSBC 16 kHz" if self.codec == "msbc" else "CVSD 8 kHz"
        return tr("chiamata", "call") + f" ({codec})"

    def status_line(self) -> str:
        if not self.enabled:
            return tr("Microfono delle cuffie: disattivato", "Headset microphone: off")
        if self.mic_key is None:
            return tr("Microfono virtuale non trovato (installa Hi-Fi Cable)",
                      "No virtual microphone (install Hi-Fi Cable)")
        if self.in_call:
            return tr("Microfono attivo su ", "Microphone live on ") + self.mic_key
        if self.ready:
            return tr("Microfono pronto: ", "Microphone ready: ") + self.mic_key
        return tr("Microfono: in attesa delle cuffie", "Microphone: waiting for the headset")
