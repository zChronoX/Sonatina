"""HFP microphone feasibility probe (point 5, stage A).

Answers one question on real hardware before the microphone is built into Sonatina:
does SCO audio flow over the WinUSB dongle (USB isochronous endpoints, libusb >= 1.0.27)?

What it does, with Sonatina closed (the dongle is exclusive):
  1. opens the dongle with SCO enabled, using the libusb 1.0.29 shipped in third_party/
  2. connects to the headset (pairing keys of Sonatina are reused), opens HFP as the
     Audio Gateway (the phone role), negotiates CVSD (narrow band, the controller codes it)
  3. opens an eSCO link: plays a 440 Hz tone in the headset and records its microphone for
     8 seconds to %LOCALAPPDATA%\\Sonatina\\hfp_probe_mic.wav
  4. prints what happened at each step

Usage (from sonatina\\host):
    .venv\\Scripts\\python.exe tools\\hfp_probe.py                 # preferred headset, auto SCO alt
    .venv\\Scripts\\python.exe tools\\hfp_probe.py --address 2C:BE:EE:4E:ED:2E --alt 2
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import math
import os
import struct
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import sonatina_host as sh  # noqa: E402

log = logging.getLogger("probe")


def use_bundled_libusb() -> str | None:
    """Bumble loads libusb from libusb_package (1.0.26: no isochronous on associated
    interfaces). Point it at our 1.0.29 instead."""
    path = os.path.join(os.path.dirname(HERE), "third_party", "libusb", "libusb-1.0.dll")
    if not os.path.exists(path):
        return None
    import libusb_package
    libusb_package.get_library_path = lambda: path
    return path


def install_safe_iso() -> None:
    """usb1.USBTransfer.iterISO reads raw addresses (buffer_at) with lengths taken from
    the iso descriptors. A bad descriptor makes it read outside the buffer: on Windows the
    access violation is swallowed by ctypes around libusb_handle_events and Python dies
    with 'Fatal Python error: _PyThreadState_Attach'. Read from the transfer's own Python
    buffer instead, clamp the lengths, and report what the descriptors contained."""
    import usb1
    from usb1 import libusb1

    # libusb 1.0.29 (windows_winusb.c, winusbx_copy_transfer_data) rewrites each
    # descriptor's length as Offset[i+1] - Offset[i]; WinUSB leaves later Offsets at 0,
    # so those lengths are garbage (e.g. -2401). Packet i is always at i * slot.
    seen = {"transfers": 0, "with_data": 0, "anomalies": 0}

    def report(text: str) -> None:
        if seen["anomalies"] < 8:
            print(f"  [iso] {text}", flush=True)
        seen["anomalies"] += 1

    def iterISO(self):
        transfer_p = self._USBTransfer__transfer
        transfer = transfer_p.contents
        data = self.getBuffer()
        size = len(data)
        count = transfer.num_iso_packets
        seen["transfers"] += 1
        if count <= 0 or count > 1024 or size % count:
            report(f"num_iso_packets {count}, buffer {size}: transfer skipped")
            return
        slot = size // count
        descs = list(libusb1.get_iso_packet_list(transfer_p))
        actuals = [d.actual_length for d in descs]
        if any(actuals) and seen["with_data"] < 3:
            seen["with_data"] += 1
            full = [i for i, n in enumerate(actuals) if n]
            sizes = sorted(set(actuals))
            print(f"  [iso] IN transfer {seen['transfers']}: slot {slot}, packets with data "
                  f"{len(full)}/{count} (first {full[0]}, last {full[-1]}), sizes {sizes[:6]}, "
                  f"total {sum(actuals)}", flush=True)
        for i, d in enumerate(descs):
            actual = d.actual_length
            if actual > slot:
                report(f"packet {i}: actual {actual} > slot {slot}: clamped")
                actual = slot
            start = i * slot
            yield d.status, bytearray(data[start:start + actual])

    usb1.USBTransfer.iterISO = iterISO


def legacy_setup_command():
    """HCI Setup Synchronous Connection (7.1.26): not in Bumble, and the Realtek
    RTL8761B does not implement the Enhanced variant."""
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

    return HCI_Setup_Synchronous_Connection_Command


VOICE_CVSD_16BIT = 0x0060   # linear PCM in, 2's complement, 16 bit, CVSD on air
# (name, max latency ms, retransmission effort, packet types) - HFP 1.8 table 5.8
CVSD_SETTINGS = [
    ("eSCO S4", 0x000C, 0x02, 0x0388),   # EV3 + 2-EV3
    ("eSCO S1", 0x0007, 0x01, 0x0388),
    ("SCO D1", 0xFFFF, 0x00, 0x03C4),    # HV3
]


def step(text: str) -> None:
    print(f"\n=== {text}", flush=True)


async def run(args) -> int:
    from bumble import hci, hfp, rfcomm
    from bumble.core import PhysicalTransport
    from bumble.device import Device, DeviceConfiguration
    from bumble.transport import open_transport

    settings = sh.load_settings(sh.SETTINGS_PATH)
    address = sh.norm_addr(args.address or settings.address)
    if not address:
        print("No headset: pass --address")
        return 2
    spec = f"{args.transport or settings.transport}+sco={args.alt}"
    lib = use_bundled_libusb()
    step(f"libusb: {lib or 'libusb_package (old, isochronous may fail)'}")
    if not args.raw_iso:
        install_safe_iso()
    step(f"opening {spec}")
    async with await open_transport(spec) as (hci_source, hci_sink):
        sh.apply_transport_quirks(spec.split("+")[0], hci_source)
        config = DeviceConfiguration()
        config.name = "Sonatina"
        config.keystore = "JsonKeyStore"
        config.classic_enabled = True
        config.le_enabled = False
        config.classic_sc_enabled = False
        config.classic_smp_enabled = False
        device = Device.from_config_with_hci(config, hci_source, hci_sink)

        ag_config = hfp.AgConfiguration(
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
            supported_audio_codecs=[hfp.AudioCodec.CVSD, hfp.AudioCodec.MSBC],
        )
        ag_ready: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_dlc(dlc) -> None:
            ag = hfp.AgProtocol(dlc, ag_config)
            ag.on("slc_complete", lambda: ag_ready.done() or ag_ready.set_result(ag))
            print("  headset opened HFP itself", flush=True)

        rfcomm_server = rfcomm.Server(device)
        channel = rfcomm_server.listen(on_dlc)
        headset_mux = asyncio.get_running_loop().create_future()

        def on_mux(multiplexer) -> None:
            print("  headset opened an RFCOMM session", flush=True)
            if not headset_mux.done():
                headset_mux.set_result(multiplexer)

        rfcomm_server.on("start", on_mux)
        device.sdp_service_records = {1: hfp.make_ag_sdp_records(1, channel, ag_config)}
        await device.power_on()
        step(f"controller up: {device.public_address}")

        step(f"connecting to {settings.headsets.get(address, {}).get('name', address)} {address}")
        connection = await device.connect(address, transport=PhysicalTransport.BR_EDR)
        try:
            await connection.authenticate()
        except Exception as error:
            print(f"  authenticate: {error!r}")
        if not connection.is_encrypted:
            await connection.encrypt()
        print("  link encrypted", flush=True)

        step("HFP service level connection")
        try:
            ag = await asyncio.wait_for(asyncio.shield(ag_ready), args.wait)
        except asyncio.TimeoutError:
            ag = None
        if ag is None and headset_mux.done():
            # only one RFCOMM session per link: the headset owns it, wait for its HFP
            print("  waiting for the headset to finish HFP on its session ...", flush=True)
            try:
                ag = await asyncio.wait_for(asyncio.shield(ag_ready), 15.0)
            except asyncio.TimeoutError:
                print("\nRESULT: SLC NOT WORKING (headset session open, no HFP SLC within 15 s)")
                return 1
        if ag is None:
            record = await hfp.find_hf_sdp_record(connection)
            if not record:
                print("  !!! the headset has no Hands-Free service")
                return 1
            hf_channel, version, features = record
            print(f"  HF version {version}, features {features!r}: opening RFCOMM {hf_channel}", flush=True)
            try:
                multiplexer = await asyncio.wait_for(rfcomm.Client(connection).start(), 10.0)
                print("  RFCOMM session up", flush=True)
                dlc = await asyncio.wait_for(multiplexer.open_dlc(hf_channel), 10.0)
                print(f"  DLC {hf_channel} open", flush=True)
                proto = hfp.AgProtocol(dlc, ag_config)
                proto.on("slc_complete", lambda: ag_ready.done() or ag_ready.set_result(proto))
                ag = await asyncio.wait_for(asyncio.shield(ag_ready), 10.0)
            except asyncio.TimeoutError:
                if ag_ready.done():
                    ag = ag_ready.result()
                else:
                    which = "session opened by headset" if headset_mux.done() else "no session"
                    print(f"\nRESULT: SLC NOT WORKING (RFCOMM/HFP timeout, {which})")
                    return 1
        codecs = [c.name for c in getattr(ag, "supported_audio_codecs", [])]
        print(f"  SLC up, headset codecs: {codecs or ['CVSD']}", flush=True)

        step("codec: CVSD (controller-side coding, 8 kHz)")
        if ag.supports_hf_feature(hfp.HfFeature.CODEC_NEGOTIATION):
            await asyncio.wait_for(ag.negotiate_codec(hfp.AudioCodec.CVSD), 5.0)
            print("  negotiated", flush=True)

        rx = bytearray()
        stats = {"rx": 0, "tx": 0, "bad": 0, "sizes": set()}
        phase = [0]
        sco_up: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_sco_packet(packet) -> None:
            stats["rx"] += 1
            stats["sizes"].add(len(packet.data))
            if packet.packet_status != 0:
                stats["bad"] += 1
            rx.extend(packet.data)
            # answer with the same amount of tone: the headset's clock paces us
            n = len(packet.data) // 2
            tone = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * (phase[0] + i) / 8000)))
                            for i in range(n))
            phase[0] += n
            device.host.send_sco_sdu(connection_handle=packet.connection_handle, sdu=tone)
            stats["tx"] += 1

        def on_sco_connection(link) -> None:
            link.sink = on_sco_packet
            print(f"  SCO link up: handle 0x{link.handle:04X}, type {link.link_type}, "
                  f"air mode {link.air_mode!r}, rx {link.rx_packet_length} tx {link.tx_packet_length}",
                  flush=True)
            if not sco_up.done():
                sco_up.set_result(link)

        def on_sco_failure(*error) -> None:
            if not sco_up.done():
                sco_up.set_exception(RuntimeError(f"SCO setup failed: {error}"))

        device.on("sco_connection", on_sco_connection)
        device.on("sco_connection_failure", on_sco_failure)

        enhanced = device.host.supports_command(hci.HCI_ENHANCED_SETUP_SYNCHRONOUS_CONNECTION_COMMAND)
        legacy = device.host.supports_command(hci.HCI_SETUP_SYNCHRONOUS_CONNECTION_COMMAND)
        step(f"synchronous link (controller: enhanced setup {'yes' if enhanced else 'no'}, "
             f"setup {'yes' if legacy else 'no'})")
        link = None
        if enhanced:
            params = hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_CVSD_S4]
            await device.send_command(hci.HCI_Enhanced_Setup_Synchronous_Connection_Command(
                connection_handle=connection.handle, **params.asdict()))
            link = await asyncio.wait_for(sco_up, 10.0)
        else:
            try:
                await device.send_command(hci.HCI_Write_Voice_Setting_Command(
                    voice_setting=VOICE_CVSD_16BIT))
            except Exception as error:
                print(f"  write voice setting: {error!r}")
            setup = legacy_setup_command()
            for name, latency, effort, packets in CVSD_SETTINGS:
                if sco_up.done():
                    sco_up = asyncio.get_running_loop().create_future()
                print(f"  trying {name} ...", flush=True)
                try:
                    await device.send_command(setup(
                        connection_handle=connection.handle, transmit_bandwidth=8000,
                        receive_bandwidth=8000, max_latency=latency,
                        voice_setting=VOICE_CVSD_16BIT, retransmission_effort=effort,
                        packet_type=packets))
                    link = await asyncio.wait_for(asyncio.shield(sco_up), 6.0)
                    break
                except Exception as error:
                    print(f"  {name} failed: {error!r}", flush=True)
            if link is None:
                print("\nRESULT: SCO NOT WORKING (no synchronous link)")
                return 1

        step("8 s: you should hear a 440 Hz tone; talk into the headset microphone")
        t0 = time.monotonic()
        last = 0
        while time.monotonic() - t0 < args.seconds:
            await asyncio.sleep(1.0)
            print(f"  {time.monotonic() - t0:4.1f} s: packets in {stats['rx']} (+{stats['rx'] - last}/s), "
                  f"out {stats['tx']}, errors {stats['bad']}, sizes {sorted(stats['sizes'])}", flush=True)
            last = stats["rx"]
        await link.disconnect()

        out = os.path.join(sh.APP_DIR, "hfp_probe_mic.wav")
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(bytes(rx))
        peak = max((abs(v) for (v,) in struct.iter_unpack("<h", bytes(rx[: len(rx) // 2 * 2]))), default=0)
        step(f"microphone: {len(rx) // 2 / 8000:.1f} s recorded, peak {peak}, saved to {out}")
        ok = stats["rx"] > 100 * args.seconds and peak > 300
        print("\nRESULT:", "SCO OK" if ok else "SCO NOT WORKING", flush=True)
        await connection.disconnect()
        return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address")
    parser.add_argument("--transport")
    parser.add_argument("--alt", type=int, default=2,
                        help="USB alternate setting for SCO: 2 = one 16-bit CVSD link (as Linux btusb), 0 = largest")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--wait", type=float, default=8.0,
                        help="seconds to wait for the headset to open HFP itself")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--raw-iso", action="store_true", help="keep usb1's own iterISO (crashes)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
