"""SCO over USB for the headset microphone (split from sonatina_hfp in 1.3.0).

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

import asyncio
import collections
import dataclasses
import logging
import os
import sys
import time

log = logging.getLogger("sonatina.hfp")

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
