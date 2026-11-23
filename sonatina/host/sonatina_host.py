"""SonatinaHost (phase U1): Windows audio -> LDAC/SBC -> A2DP over a dedicated USB dongle.

Windows apps play to VB-CABLE ("CABLE Input"); this program captures "CABLE Output",
encodes with the Sonatina codecs (sonatina_codec.dll, same encoder code as the kernel
driver) and streams to the headset through a user-mode Bluetooth host (Bumble) that
drives a USB dongle bound to WinUSB. No kernel component of ours: Secure Boot stays on.

Usage (from this folder):
    python sonatina_host.py                      # LDAC to the configured headset
    python sonatina_host.py --codec sbc
    python sonatina_host.py --tone               # 1 kHz test tone instead of VB-CABLE
    python sonatina_host.py --list-devices       # audio capture devices
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import sys
import time

from bumble.a2dp import (
    A2DP_MPEG_2_4_AAC_CODEC_TYPE,
    A2DP_NON_A2DP_CODEC_TYPE,
    A2DP_SBC_CODEC_TYPE,
    SbcMediaCodecInformation,
    VendorSpecificMediaCodecInformation,
    make_audio_source_service_sdp_records,
)
from bumble.avdtp import (
    AVDTP_AUDIO_MEDIA_TYPE,
    AVDTP_DELAY_REPORTING_SERVICE_CATEGORY,
    MediaCodecCapabilities,
    find_avdtp_service_with_connection,
)
from bumble.avdtp import Protocol as AvdtpProtocol
from bumble.core import AdvertisingData, DeviceClass, PhysicalTransport
from bumble.device import Connection, Device, DeviceConfiguration
from bumble.pairing import PairingConfig

import sonatina_avrcp
import sonatina_hfp
import sonatina_media
import sonatina_output
import sonatina_radio
from sonatina_common import (
    AAC_FRAME,
    AAC_LATM_OVERHEAD,
    APP_DIR,
    app_version,
    APTX_CODEC_ID,
    APTX_HD_CODEC_ID,
    APTX_HD_VENDOR_ID,
    APTX_VENDOR_ID,
    CHANNELS,
    CODEC_CHAIN,
    CODEC_NAMES,
    com_init,
    FRAME_BYTES,
    HERE,
    IPC_PATH,
    LDAC_CODEC_ID,
    LDAC_DELAY_MS,
    LDAC_EQMID,
    LDAC_KBPS,
    LDAC_VENDOR_ID,
    LOW_LATENCY_PACKET_MS,
    LOW_LATENCY_TARGET_MS,
    norm_addr,
    NORMAL_TARGET_MS,
    PUSH_SAMPLES,
    pushes_for,
    RATE,
    RTP_HEADER,
    SETTINGS_PATH,
    SSRC,
    tr,
    UI_ITALIAN,
    VENDOR_CODECS,
)
from sonatina_codecs import (
    aac_config,
    aac_sink_usable,
    AacEncoder,
    aptx_config,
    AptxEncoder,
    CodecLib,
    ldac_config,
    LdacEncoder,
    sbc_config,
    SbcEncoder,
)
from sonatina_settings import (
    load_settings,
    save_settings,
    SAVED_FIELDS,
    Settings,
)
from sonatina_audio import (
    CableCapture,
    EndpointVolume,
    find_capture_device,
    fix_default_output,
    LdacAdapter,
    LinkBacklog,
    MediaPump,
    not_silent,
    PcmRing,
    set_default_output,
    SILENCE,
    ToneSource,
)

log = logging.getLogger("sonatina")


# Kept importable as sonatina_host.<name> (tray, dashboard channel, tests).
REEXPORTED = (
    AAC_FRAME,
    AAC_LATM_OVERHEAD,
    APP_DIR,
    app_version,
    APTX_CODEC_ID,
    APTX_HD_CODEC_ID,
    APTX_HD_VENDOR_ID,
    APTX_VENDOR_ID,
    CHANNELS,
    CODEC_CHAIN,
    CODEC_NAMES,
    com_init,
    FRAME_BYTES,
    HERE,
    IPC_PATH,
    LDAC_CODEC_ID,
    LDAC_DELAY_MS,
    LDAC_EQMID,
    LDAC_KBPS,
    LDAC_VENDOR_ID,
    LOW_LATENCY_PACKET_MS,
    LOW_LATENCY_TARGET_MS,
    norm_addr,
    NORMAL_TARGET_MS,
    PUSH_SAMPLES,
    pushes_for,
    RATE,
    RTP_HEADER,
    SETTINGS_PATH,
    SSRC,
    tr,
    UI_ITALIAN,
    VENDOR_CODECS,
    aac_config,
    aac_sink_usable,
    AacEncoder,
    aptx_config,
    AptxEncoder,
    CodecLib,
    ldac_config,
    LdacEncoder,
    sbc_config,
    SbcEncoder,
    load_settings,
    save_settings,
    SAVED_FIELDS,
    Settings,
    CableCapture,
    EndpointVolume,
    find_capture_device,
    fix_default_output,
    LdacAdapter,
    LinkBacklog,
    MediaPump,
    not_silent,
    PcmRing,
    set_default_output,
    SILENCE,
    ToneSource,
)


AUDIO_VIDEO_MAJOR_CLASS = 0x04


def connect_failure_reason(error: BaseException) -> str:
    """The HCI reason of a failed page (PAGE_TIMEOUT_ERROR: headphones off or busy)."""
    name = getattr(error, "error_name", "") or ""
    code = getattr(error, "error_code", None)
    if name:
        return name
    if isinstance(code, int):
        return f"0x{code:02X}"
    return type(error).__name__


class SonatinaHost:
    def __init__(self, settings: Settings, codec_lib: CodecLib | None = None) -> None:
        self.s = settings
        self.lib = codec_lib or CodecLib()
        self.ring = PcmRing()
        self.volume = EndpointVolume(self.lib, settings.volume_endpoint)
        self.remote = sonatina_avrcp.RemoteControl(self, sonatina_media.sessions())
        self.connection: Connection | None = None
        self.session_task: asyncio.Task | None = None
        self.pump: MediaPump | None = None
        self.state = "idle"
        self.peer: str | None = None          # address of the connected headset
        self.paired: set[str] = set()
        self.pairing_target: str | None = None
        self.pairing_done = asyncio.Event()
        self.pairing_done.set()
        self.discovering = False
        self.discovered: dict[str, str] = {}  # address -> name
        self.headset_delay_ms: float | None = None
        self.on_change = None                 # called after settings changed (save)
        self.ready = False                    # True once powered on and keys loaded
        self.stream = None                    # the A2DP stream (suspended during calls)
        self.codec_in_use: str | None = None  # key in CODEC_CHAIN of the running stream
        self.hfp = sonatina_hfp.HfpGateway(self)
        self.output = sonatina_output.OutputSwitcher(self)
        self.radio = sonatina_radio.RadioMonitor(self)
        self.on_notice = None                 # tray balloon (text)
        self.battery: int | None = None       # headset battery %, this connection
        self.battery_source: str | None = None
        self.battery_warned = 101             # lowest threshold already announced

    # -- settings ---------------------------------------------------------------
    def headset_name(self, address: str | None) -> str:
        if not address:
            return "-"
        return self.s.headsets.get(address, {}).get("name") or address

    def codec_for(self, address: str) -> tuple[str, str]:
        h = self.s.headsets.get(address, {})
        return h.get("codec", self.s.codec), h.get("ldac_quality", self.s.ldac_quality)

    def _changed_state(self) -> None:
        """Something the tray shows changed (it polls every second: nothing to do)."""

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self.s)
            except Exception as error:
                log.warning("saving settings failed: %r", error)

    async def refresh_paired(self) -> None:
        self.paired = set()
        if self.device.keystore is not None:
            for name, _keys in await self.device.keystore.get_all():
                self.paired.add(norm_addr(name))
        changed = False
        for address in self.paired:
            if address not in self.s.headsets:
                self.s.headsets[address] = {}
                changed = True
        if not self.s.address and self.paired:
            self.s.address = sorted(self.paired)[0]
            changed = True
        if changed:
            self._changed()

    async def after_power_on(self) -> None:
        """Connectable for paired headsets, never discoverable (nobody else can find us)."""
        await self.device.set_discoverable(False)
        await self.device.set_connectable(True)
        await self.refresh_paired()
        self.ready = True

    # -- device setup ---------------------------------------------------------
    @staticmethod
    def configure_device(device: Device) -> None:
        device.sdp_service_records = {
            0x00010001: make_audio_source_service_sdp_records(0x00010001),
            **sonatina_avrcp.sdp_records(0x00010002, 0x00010003),
        }
        device.pairing_config_factory = lambda connection: PairingConfig(mitm=False)

    def attach(self, device: Device) -> None:
        self.device = device
        self.configure_device(device)
        self.hfp.attach(device)
        self.remote.listen(device)
        device.on("connection", self._on_connection)

    def _on_connection(self, connection: Connection) -> None:
        if connection.transport != PhysicalTransport.BR_EDR:
            return
        peer = norm_addr(connection.peer_address)
        loop = asyncio.get_running_loop()
        known = peer in self.paired or peer == self.pairing_target or self.s.allow_unknown
        if not known or self.connection is not None:
            log.warning("refusing %s (%s)", peer, "busy" if known else "not paired")
            loop.create_task(connection.disconnect())
            return
        log.info("connected: %s %s (we are %s)", self.headset_name(peer), peer,
                 "CENTRAL" if connection.role == 0 else "PERIPHERAL")
        self.connection = connection
        self.peer = peer
        connection.on("disconnection", self._on_disconnection)
        self.hfp.on_connected(connection)
        self.remote.start(connection)
        self.session_task = loop.create_task(self._session(connection, peer))

    def _on_delay_report(self, delay: int) -> None:
        """AVDTP delay report, in 1/10 ms: the headset's own buffer + decoding."""
        self.headset_delay_ms = delay / 10
        log.info("headset delay %.1f ms", self.headset_delay_ms)

    def latency_ms(self) -> int | None:
        """Estimated delay from Windows to the ears (None while not streaming)."""
        if self.state != "streaming" or self.pump is None or self.pump.encoder is None:
            return None
        return self.pump.latency_ms() + int(self.headset_delay_ms or 0)

    # -- battery (1.2.0) ----------------------------------------------------------
    BATTERY_WARN = (15, 5)

    def on_battery(self, percent: int, source: str) -> None:
        """Battery level reported by the headset (HFP)."""
        percent = max(0, min(100, int(percent)))
        old = self.battery
        self.battery, self.battery_source = percent, source
        if old is None or abs(percent - old) >= 10 or percent <= 20 and percent != old:
            log.info("headset battery %d%% (%s)", percent, source)
        if self.peer:
            entry = self.s.headsets.setdefault(self.peer, {})
            if entry.get("battery") != percent:
                entry["battery"], entry["battery_at"] = percent, int(time.time())
                self._changed()
        if percent >= 25:
            self.battery_warned = 101      # charged again: warn again next time
        for level in self.BATTERY_WARN:
            if percent <= level < self.battery_warned:
                self.battery_warned = level
                if self.on_notice is not None:
                    self.on_notice(tr(f"Batteria delle cuffie al {percent}%", f"Headphone battery at {percent}%")
                                   + (f": {self.headset_name(self.peer)}" if self.peer else ""))
                break

    def battery_text(self) -> str:
        return "" if self.battery is None else tr(" · batteria ", " · battery ") + f"{self.battery}%"

    def _on_disconnection(self, reason) -> None:
        log.info("disconnected (reason 0x%02X)", reason)
        self.battery = None
        self.battery_warned = 101
        self.connection = None
        self.peer = None
        self.state = "idle"
        self.stream = None
        self.codec_in_use = None
        self.hfp.on_disconnected()
        self.remote.stop()
        if self.pump:
            asyncio.get_running_loop().create_task(self.pump.stop())
        if self.session_task and not self.session_task.done():
            self.session_task.cancel()

    async def _secure(self, connection: Connection) -> None:
        # Lesson from U0: encryption alone hangs on reconnect, authenticate first.
        if not connection.is_encrypted:
            try:
                await connection.authenticate()
            except Exception as error:  # the headset may already be authenticating
                log.debug("authenticate: %s", error)
            if not connection.is_encrypted:
                await connection.encrypt()

    async def _session(self, connection: Connection, peer: str) -> None:
        try:
            self.state = "securing"
            if peer == self.pairing_target:
                await self.pairing_done.wait()   # pair() authenticates this link
            await self._secure(connection)
            if not self.s.headsets.get(peer, {}).get("name"):
                try:
                    name = await asyncio.wait_for(connection.request_remote_name(), 5)
                    self.s.headsets.setdefault(peer, {})["name"] = name
                    self._changed()
                    log.info("headset name: %s", name)
                except Exception as error:
                    log.debug("remote name: %r", error)
            self.state = "negotiating"
            version = await find_avdtp_service_with_connection(connection) or (1, 3)
            protocol = await AvdtpProtocol.connect(connection, version)
            await protocol.discover_remote_endpoints()

            mtu_holder = {"mtu": 679}
            codec, ldac_quality = self.codec_for(peer)
            log.info("headset codecs: %s", ", ".join(self._sink_names(protocol)) or "?")
            usable = [k for k in CODEC_CHAIN if self._choose(protocol, k, ldac_quality, mtu_holder) is not None]
            entry = self.s.headsets.setdefault(peer, {})
            if entry.get("codecs") != usable:      # remembered for the dashboard
                entry["codecs"] = usable
                self._changed()
            choice = None
            for candidate in CODEC_CHAIN[CODEC_CHAIN.index(codec) if codec in CODEC_CHAIN else 0:]:
                choice = self._choose(protocol, candidate, ldac_quality, mtu_holder)
                if choice is not None:
                    self.codec_in_use = candidate
                    break
                log.info("%s not usable with this headset, trying the next codec",
                         CODEC_NAMES[candidate])
            if choice is None:
                raise RuntimeError("no usable sink on the headset (not even SBC)")

            sink, config, make_encoder = choice
            delay_reporting = any(
                c.service_category == AVDTP_DELAY_REPORTING_SERVICE_CATEGORY for c in sink.capabilities)
            self.pump = MediaPump(self.ring, make_encoder, self.volume, self.jitter_target_ms())
            self.pump.adaptive = bool(self.s.ldac_adaptive)
            self.headset_delay_ms = None
            source = protocol.add_source(config, self.pump, delay_reporting)
            source.on("delay_report", self._on_delay_report)
            stream = await protocol.create_stream(source, sink)
            await stream.open()
            if stream.rtp_channel is not None:
                mtu_holder["mtu"] = stream.rtp_channel.peer_mtu
                log.info("media channel MTU %d", mtu_holder["mtu"])
            await stream.start()
            self.stream = stream
            self.state = "streaming"
            self.remote.on_streaming()
            self.hfp.start_slc()
            if self.s.address != peer:   # the headset in use becomes the active one
                self.s.address = peer
                self._changed()
            await asyncio.get_running_loop().create_future()  # until disconnection
        except asyncio.CancelledError:
            pass
        except Exception as error:
            log.error("session failed: %r", error)
            self.state = "error"
            try:
                await connection.disconnect()
            except Exception:
                pass

    def _choose(self, protocol, codec: str, ldac_quality: str, mtu_holder: dict):
        """(sink, configuration, encoder factory) for `codec`, or None if not usable."""
        mtu = lambda: mtu_holder["mtu"]  # noqa: E731 - known only once the stream is open
        if codec == "ldac":
            sink = protocol.find_remote_sink_by_codec(
                AVDTP_AUDIO_MEDIA_TYPE, A2DP_NON_A2DP_CODEC_TYPE, LDAC_VENDOR_ID, LDAC_CODEC_ID)
            if sink is None:
                return None
            eqmid = LDAC_EQMID.get(ldac_quality, 1)
            return sink, ldac_config(), lambda: LdacEncoder(self.lib, eqmid, mtu())
        if codec in ("aptx_hd", "aptx"):
            hd = codec == "aptx_hd"
            vendor, codec_id = (APTX_HD_VENDOR_ID, APTX_HD_CODEC_ID) if hd else (APTX_VENDOR_ID,
                                                                                APTX_CODEC_ID)
            sink = protocol.find_remote_sink_by_codec(
                AVDTP_AUDIO_MEDIA_TYPE, A2DP_NON_A2DP_CODEC_TYPE, vendor, codec_id)
            info = self._codec_info(sink)
            value = bytes(getattr(info, "value", b"") or b"")
            if sink is None or not self.lib.has_aptx:
                return None
            if value and (value[0] & 0x12) != 0x12:   # 48 kHz + stereo
                log.info("%s sink without 48 kHz stereo (0x%02X)", CODEC_NAMES[codec], value[0])
                return None
            cap = self.max_packet_ms()
            return sink, aptx_config(hd), lambda: AptxEncoder(self.lib, hd, mtu(), cap)
        if codec == "aac":
            sink = protocol.find_remote_sink_by_codec(AVDTP_AUDIO_MEDIA_TYPE,
                                                      A2DP_MPEG_2_4_AAC_CODEC_TYPE)
            info = self._codec_info(sink)
            if sink is None or not aac_sink_usable(info) or not self.lib.has_aac:
                return None
            config = aac_config(info, self.s.aac_bitrate)
            bitrate = config.media_codec_information.bitrate
            return sink, config, lambda: AacEncoder(self.lib, bitrate, mtu())
        sink = protocol.find_remote_sink_by_codec(AVDTP_AUDIO_MEDIA_TYPE, A2DP_SBC_CODEC_TYPE)
        if sink is None:
            return None
        info = self._codec_info(sink)
        remote_max = info.maximum_bitpool_value if isinstance(info, SbcMediaCodecInformation) else 53
        bitpool = max(2, min(self.s.sbc_max_bitpool, remote_max))
        cap = self.max_packet_ms()
        return sink, sbc_config(bitpool), lambda: SbcEncoder(self.lib, bitpool, mtu(), cap)

    async def suspend_media(self) -> None:
        """A2DP off for a call (AVDTP SUSPEND): the headset can only do one at a time."""
        from bumble.avdtp import State
        stream = self.stream
        if stream is not None and stream.state == State.STREAMING:
            try:
                await asyncio.wait_for(stream.stop(), 3.0)
            except Exception as error:
                log.debug("A2DP suspend: %r", error)
        if self.pump is not None:
            await self.pump.stop()

    async def resume_media(self) -> None:
        from bumble.avdtp import State
        stream = self.stream
        if self.connection is None or stream is None:
            return
        if stream.state == State.STREAMING:
            if self.pump is not None and self.pump.task is None and stream.rtp_channel is not None:
                await self.pump.start(stream.rtp_channel)
            return
        try:
            await asyncio.wait_for(stream.start(), 5.0)
            log.info("A2DP resumed")
        except Exception as error:
            log.warning("A2DP resume failed (%r): reconnecting", error)
            try:
                await self.connection.disconnect()
            except Exception:
                pass

    def max_packet_ms(self) -> int:
        """Audio waits until its packet is full: smaller packets in low-latency mode."""
        return LOW_LATENCY_PACKET_MS if self.s.low_latency else 0

    def jitter_target_ms(self) -> int:
        return LOW_LATENCY_TARGET_MS if self.s.low_latency else NORMAL_TARGET_MS

    @staticmethod
    def _sink_names(protocol) -> list[str]:
        """Codecs the headset offers, for the log (helps when adding headsets)."""
        names = []
        for endpoint in protocol.remote_endpoints.values():
            for cap in endpoint.capabilities:
                if not isinstance(cap, MediaCodecCapabilities):
                    continue
                info = cap.media_codec_information
                if cap.media_codec_type == A2DP_SBC_CODEC_TYPE:
                    names.append("SBC")
                elif cap.media_codec_type == A2DP_MPEG_2_4_AAC_CODEC_TYPE:
                    names.append("AAC")
                elif isinstance(info, VendorSpecificMediaCodecInformation):
                    key = (info.vendor_id, info.codec_id)
                    names.append(VENDOR_CODECS.get(key, f"vendor 0x{key[0]:04X}/0x{key[1]:04X}"))
                else:
                    names.append(f"type 0x{cap.media_codec_type:02X}")
        return names

    @staticmethod
    def _codec_info(sink):
        if sink is None:
            return None
        for cap in sink.capabilities:
            if isinstance(cap, MediaCodecCapabilities):
                return cap.media_codec_information
        return None

    # -- connection management -------------------------------------------------
    async def keep_connected(self) -> None:
        """Wait a little for a headset to connect to us, then page the preferred one;
        retry forever. Any paired headset that connects by itself is accepted too."""
        await asyncio.sleep(3)
        backoff = 3.0
        failures = 0
        next_summary = 0.0
        while True:
            busy = self.discovering or self.pairing_target is not None
            if self.connection is None and self.s.address and not busy:
                # the first attempts in full, then one line every 5 minutes (2026-11-22:
                # 200 identical lines in 17 minutes said nothing about the cause)
                verbose = failures < 4
                try:
                    if verbose:
                        log.info("connecting to %s ...", self.headset_name(self.s.address))
                    await self.device.connect(self.s.address, transport=PhysicalTransport.BR_EDR)
                    backoff = 3.0
                    failures = 0
                except Exception as error:
                    failures += 1
                    reason = connect_failure_reason(error)
                    now = time.monotonic()
                    if verbose:
                        log.info("connect failed (%s), retry in %.0f s", reason, backoff)
                        next_summary = now + 300.0
                    elif now >= next_summary:
                        next_summary = now + 300.0
                        log.info("still not connected to %s: %d attempts, last %s%s",
                                 self.headset_name(self.s.address), failures, reason,
                                 " (headphones off or connected to another device?)"
                                 if "TIMEOUT" in reason.upper() else "")
                    backoff = min(backoff * 1.5, 20.0)
            elif self.connection is not None:
                failures = 0
            await asyncio.sleep(backoff if self.connection is None else 2.0)

    # -- headset management (called from the tray) ------------------------------
    async def discover(self, seconds: float = 12.0) -> dict[str, str]:
        """Looks for headsets in pairing mode (Audio/Video class of device)."""
        self.discovering = True
        self.discovered = {}
        unnamed: list = []

        def on_result(address, class_of_device, data, rssi) -> None:
            _services, major, _minor = DeviceClass.split_class_of_device(class_of_device)
            if major != AUDIO_VIDEO_MAJOR_CLASS:
                return
            key = norm_addr(address)
            name = None
            try:
                name = data.get(AdvertisingData.COMPLETE_LOCAL_NAME) or data.get(
                    AdvertisingData.SHORTENED_LOCAL_NAME)
            except Exception:
                pass
            if key not in self.discovered:
                log.info("found %s %s", name or "?", key)
                if not name:
                    unnamed.append(address)
            self.discovered[key] = str(name) if name else self.discovered.get(key, key)

        self.device.on("inquiry_result", on_result)
        try:
            await self.device.start_discovery(auto_restart=False)
            await asyncio.sleep(seconds)
            await self.device.stop_discovery()
            for address in unnamed:
                try:
                    name = await asyncio.wait_for(self.device.request_remote_name(address), 5)
                    self.discovered[norm_addr(address)] = name
                except Exception:
                    pass
        finally:
            self.device.remove_listener("inquiry_result", on_result)
            self.discovering = False
        return dict(self.discovered)

    async def pair(self, address: str, name: str | None = None) -> bool:
        address = norm_addr(address)
        self.pairing_target = address
        self.pairing_done.clear()
        try:
            if self.connection is not None:
                self.output.expect_reconnect()
                await self.connection.disconnect()
                await asyncio.sleep(1)
            log.info("pairing with %s %s ...", name or "?", address)
            connection = await self.device.connect(address, transport=PhysicalTransport.BR_EDR)
            await connection.authenticate()
            if not connection.is_encrypted:
                await connection.encrypt()
            await self.refresh_paired()
            entry = self.s.headsets.setdefault(address, {})
            if name:
                entry["name"] = name
            self.s.address = address
            self._changed()
            log.info("paired with %s", self.headset_name(address))
            return True
        except Exception as error:
            log.error("pairing with %s failed: %r", address, error)
            return False
        finally:
            self.pairing_target = None
            self.pairing_done.set()

    async def select(self, address: str) -> None:
        """Makes `address` the preferred headset and switches to it."""
        address = norm_addr(address)
        self.s.address = address
        self._changed()
        if self.connection is not None and self.peer != address:
            self.output.expect_reconnect()
            await self.connection.disconnect()

    async def set_codec(self, address: str, codec: str, ldac_quality: str | None = None) -> None:
        entry = self.s.headsets.setdefault(norm_addr(address), {})
        entry["codec"] = codec
        if ldac_quality:
            entry["ldac_quality"] = ldac_quality
        self._changed()
        if self.connection is not None and self.peer == norm_addr(address):
            self.output.expect_reconnect()
            await self.connection.disconnect()   # renegotiates with the new codec

    async def set_hfp_mode(self, mode: str) -> bool:
        """Returns True when the Bluetooth host must restart (HFP turned on or off)."""
        old = self.s.hfp_mode
        self.s.hfp_mode = mode
        self._changed()
        self.hfp.dismissed = False
        self.hfp.sco_failures = 0
        if mode == "auto" and self.hfp.in_call:
            pass   # the watcher decides at its next poll
        return (old == "off") != (mode == "off")

    async def set_option(self, name: str, value: bool) -> None:
        """absolute_volume and low_latency need a reconnection to take effect, media_keys not."""
        setattr(self.s, name, value)
        if name == "hfp_msbc" and value:   # asked again: give mSBC another chance everywhere
            for entry in self.s.headsets.values():
                entry.pop("msbc_broken", None)
        self._changed()
        if name == "ldac_adaptive" and self.pump is not None:
            self.pump.adaptive = bool(value)        # live, no reconnection
        if name == "auto_output" and value:
            self.output.option_changed()
        if name == "wifi_avoid":
            self.radio.option_changed()
        if name in ("absolute_volume", "low_latency", "hfp_battery") and self.connection is not None:
            self.output.expect_reconnect()
            await self.connection.disconnect()

    async def set_fallback_output(self, endpoint_id: str) -> None:
        """The output for when the headphones are off ("" = automatic)."""
        name = ""
        if endpoint_id:
            match = [e for e in self.output.outputs if e.id == endpoint_id]
            if not match:
                raise ValueError("unknown output")
            name = match[0].name
        self.s.fallback_output, self.s.fallback_output_name = endpoint_id, name
        self._changed()
        log.info("output when the headphones are off: %s", name or "automatic")
        if self.connection is None:
            self.output.option_changed()

    async def set_mic_denoise(self, mode: str) -> None:
        """Noise reduction of the headset microphone: off | normal | strong (live)."""
        if mode not in ("off", "normal", "strong"):
            raise ValueError("mode")
        self.s.mic_denoise = mode
        self._changed()
        log.info("microphone noise reduction: %s", mode)

    async def set_wifi_channel(self, channel: int) -> None:
        """Wi-Fi channel to keep Bluetooth away from, chosen by hand (0 = automatic)."""
        self.s.wifi_channel = max(0, min(14, int(channel)))
        self._changed()
        self.radio.option_changed()

    async def forget(self, address: str) -> None:
        address = norm_addr(address)
        if self.connection is not None and self.peer == address:
            await self.connection.disconnect()
        if self.device.keystore is not None:
            for name, _keys in await self.device.keystore.get_all():
                if norm_addr(name) == address:
                    await self.device.keystore.delete(name)
        self.s.headsets.pop(address, None)
        if self.s.address == address:
            self.s.address = ""
        await self.refresh_paired()
        self._changed()
        log.info("forgot %s", address)

    def audio_age(self) -> float:
        """Seconds since non-silent audio was streamed (AVRCP play/pause logic)."""
        if self.pump is None or not self.pump.last_audio:
            return 1e9
        return time.perf_counter() - self.pump.last_audio

    async def follow_volume(self) -> None:
        while True:
            self.volume.poll()
            self.remote.on_windows_volume()
            await asyncio.sleep(0.1)

    def snapshot(self) -> dict:
        """Everything the dashboard shows, as plain JSON values. Called from the dashboard
        channel's thread: only reads."""
        p = self.pump
        enc = p.encoder if p is not None else None
        hf = self.hfp
        audio = hf.audio
        headsets = []
        for address in sorted(set(self.paired) | set(self.s.headsets)):
            h = self.s.headsets.get(address, {})
            codec, quality = self.codec_for(address)
            headsets.append({
                "address": address, "name": self.headset_name(address), "codec": codec,
                "ldac_quality": quality, "supported": list(h.get("codecs", [])),
                "preferred": address == self.s.address, "connected": address == self.peer,
                "paired": address in self.paired, "msbc_broken": bool(h.get("msbc_broken")),
                "battery": h.get("battery"), "battery_at": h.get("battery_at")})
        last = self.volume.last
        apps = []
        if hf.watcher is not None:
            ignored = getattr(hf.watcher, "IGNORED", ())
            apps = [a for a in getattr(hf.watcher, "last_apps", ()) if a.lower() not in ignored]
        return {
            "state": self.state,
            "transport": self.s.transport,
            "summary": self.summary(),
            "peer": self.peer,
            "headsets": headsets,
            "discovering": self.discovering,
            "found": [{"address": a, "name": n} for a, n in sorted(self.discovered.items())
                      if a not in self.paired],
            "pairing": self.pairing_target,
            "stream": None if enc is None else {
                "codec": self.codec_in_use, "name": enc.name, "kbps": enc.kbps,
                "latency_ms": self.latency_ms(), "headset_delay_ms": self.headset_delay_ms},
            "volume": {"percent": last[1] if last else None, "muted": bool(last and last[0]),
                       "absolute": bool(getattr(self.remote, "absolute", False))},
            "options": {k: bool(getattr(self.s, k)) for k in
                        ("absolute_volume", "low_latency", "media_keys", "hfp_msbc", "ldac_adaptive",
                         "auto_output", "pause_on_disconnect", "wifi_avoid", "hfp_battery",
                         "mic_agc", "mic_gate")},
            "output": self.output.snapshot(),
            "battery": None if self.battery is None else {"percent": self.battery, "source": self.battery_source},
            "radio": self.radio.snapshot(),
            "mic": {"mode": self.s.hfp_mode, "msbc": bool(self.s.hfp_msbc), "cable": hf.mic_key,
                    "ready": hf.ready, "in_call": hf.in_call, "codec": hf.codec, "apps": apps,
                    "dismissed": hf.dismissed, "level": round(audio.level, 3) if audio else 0.0,
                    "frames_ok": audio.frames_ok if audio else 0,
                    "frames_lost": audio.frames_lost if audio else 0,
                    "voice": {"available": bool(getattr(self.lib, "has_voice", False)),
                              "denoise": self.s.mic_denoise, "agc": bool(self.s.mic_agc),
                              "gate": bool(self.s.mic_gate),
                              "level_in": round(audio.level_in, 3) if audio else 0.0,
                              "vad": round(audio.vad, 2) if audio else 0.0,
                              "gain_db": round(audio.agc_db, 1) if audio else None},
                    "status": hf.status_line()},
            "link": {"ring_ms": self.ring.level_ms(), "underruns": self.ring.underruns,
                     "target_ms": p.target_ms if p else None,
                     "margin_ms": None if p is None or p.margin_ms is None else round(p.margin_ms, 1),
                     "rate_ppm": round((p.rate - 1) * 1e6) if p else None,
                     "packets": p.packets if p else 0,
                     "radio_dropped": p.radio_dropped if p else 0,
                     "call": audio.stats() if audio else None},
        }

    def stats_line(self) -> str:
        p = self.pump
        return (f"[{self.state}] ring={self.ring.level_ms()} ms underruns={self.ring.underruns} "
                f"dropped={self.ring.dropped // FRAME_BYTES} smp"
                + (f" packets={p.packets} skips={p.skips} radio_dropped={p.radio_dropped}"
                   f" max_late={p.max_late_ms:.1f} ms"
                   f" margin={p.margin_ms if p.margin_ms is None else round(p.margin_ms, 1)} ms"
                   f" target={p.target_ms} ms rate={(p.rate - 1) * 1e6:+.0f} ppm"
                   f" latency={self.latency_ms()} ms" if p else "")
                + self.radio.stats()
                + (f" call: {self.hfp.audio.stats()}" if self.hfp.audio is not None else ""))

    def summary(self) -> str:
        """One line for the tray tooltip."""
        p = self.pump
        if self.hfp.in_call:
            return f"{self.headset_name(self.peer)}: {self.hfp.summary()}{self.battery_text()}"
        if self.state == "streaming" and p is not None and p.encoder is not None:
            vol = "" if self.volume.last is None else (
                tr(" · muto", " · muted") if self.volume.last[0] else f" · vol {self.volume.last[1]}%")
            if self.remote.absolute and vol:
                vol += tr(" (cuffie)", " (headset)")
            latency = self.latency_ms()
            if latency is not None and self.headset_delay_ms is not None:
                vol += tr(" · ritardo ~", " · delay ~") + f"{latency} ms"
            if self.radio.level == "weak":
                vol += tr(" · segnale debole", " · weak signal")
            vol += self.battery_text()
            return f"{self.headset_name(self.peer)}: {p.encoder.name} {p.encoder.kbps} kbit/s{vol}"
        if self.discovering:
            return tr("Sonatina: ricerca cuffie...", "Sonatina: looking for headphones...")
        if self.pairing_target:
            return tr("Sonatina: abbinamento con ", "Sonatina: pairing with ") + \
                f"{self.headset_name(self.pairing_target)}..."
        return {"idle": tr("Sonatina: in attesa delle cuffie", "Sonatina: waiting for the headphones"),
                "securing": tr("Sonatina: connessione...", "Sonatina: connecting..."),
                "negotiating": tr("Sonatina: connessione...", "Sonatina: connecting..."),
                "error": tr("Sonatina: errore (vedi log)", "Sonatina: error (see the log)"),
                }.get(self.state, f"Sonatina: {self.state}")

    async def print_stats(self, interval: float = 5.0, to_log: bool = False) -> None:
        last = None
        while True:
            await asyncio.sleep(interval)
            line = self.stats_line()
            if to_log:
                if line != last:
                    log.info("%s", line)
                last = line
            else:
                print(line, flush=True)


# ----------------------------------------------------------------------------- robustness
class SystemResumed(Exception):
    """The PC slept: USB, the radio link and WASAPI streams are stale. Start over."""


async def watch_suspend(interval: float = 2.0, gap: float = 10.0) -> None:
    """A 2 s sleep that takes more than 10 s means the PC was suspended (monotonic time
    keeps counting during sleep on Windows) or the process was frozen."""
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(interval)
        elapsed = time.monotonic() - t0
        if elapsed > gap:
            raise SystemResumed(f"{elapsed:.0f} s")


async def watch_capture(source) -> None:
    while True:
        await asyncio.sleep(1.0)
        source.check()


BARROT_VENDOR_IDS = ("33FA",)


class StrictEventSplitter:
    """HCI event splitter for controllers that pad USB interrupt transfers.

    Barrot (UGREEN BT 6.0, 33FA:0012) sends one spurious byte after the Read Local
    Extended Features event; Bumble's splitter, reading a byte stream, then takes it as
    the start of the next event and every later event is misparsed. Each interrupt
    transfer holds whole events, so bytes that cannot be a complete event at the end of
    a transfer are dropped here."""

    def __init__(self, emit) -> None:
        self.emit = emit
        self.dropped = 0

    def feed(self, data: bytes) -> None:
        data = bytes(data)
        while data:
            if len(data) < 2 or len(data) < 2 + data[1]:
                self.dropped += len(data)
                log.debug("HCI: dropped %d spurious byte(s) %s", len(data), data.hex())
                return
            size = 2 + data[1]
            self.emit(data[:size])
            data = data[size:]


def apply_transport_quirks(spec: str, hci_source) -> None:
    vid = spec.split(":")[1].upper() if spec.lower().startswith("usb:") and spec.count(":") >= 2 else ""
    splitters = getattr(hci_source, "splitters", None)
    if vid in BARROT_VENDOR_IDS and isinstance(splitters, dict):
        from bumble import hci as bumble_hci

        old = splitters.get(bumble_hci.HCI_EVENT_PACKET)
        if old is not None:
            splitters[bumble_hci.HCI_EVENT_PACKET] = StrictEventSplitter(old.emit)
            log.info("Barrot adapter: spurious-byte quirk on")


# ----------------------------------------------------------------------------- main
def set_timer_resolution() -> None:
    if sys.platform == "win32":
        try:
            ctypes.WinDLL("winmm").timeBeginPeriod(1)  # 1 ms sleeps for the media pump
        except OSError:
            pass


ADAPTER_STUCK = False   # a close timed out: only a new process frees the adapter


async def close_transport(transport, timeout: float = 6.0) -> None:
    """Closes the adapter even if a second restart arrives meanwhile. An interrupted close
    leaves WinUSB open inside this process and every reopen fails with
    LIBUSB_ERROR_ACCESS (2026-11-20: two menu clicks 7 s apart)."""
    t0 = time.monotonic()
    task = asyncio.ensure_future(transport.close())
    while not task.done():
        left = timeout - (time.monotonic() - t0)
        if left <= 0:
            global ADAPTER_STUCK
            ADAPTER_STUCK = True
            log.warning("adapter close still running after %.0f s", timeout)
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), left)
        except asyncio.CancelledError:
            log.info("restart requested while closing the adapter: finishing the close first")
        except asyncio.TimeoutError:
            pass
        except Exception:
            break
    if task.done() and not task.cancelled() and task.exception() is not None:
        log.warning("adapter close: %r", task.exception())
    else:
        log.info("adapter closed (%.1f s)", time.monotonic() - t0)


async def amain(settings: Settings, on_host=None, stats_to_log: bool = False) -> None:
    """Runs until the dongle goes away (unplugged) or the task is cancelled.
    on_host(host) is called once the host object exists (used by the tray)."""
    from bumble.transport import open_transport

    set_timer_resolution()
    host = SonatinaHost(settings)
    if on_host is not None:
        on_host(host)
    source = ToneSource(host.ring) if settings.tone else CableCapture(host.ring, settings.capture)

    spec = settings.transport
    if settings.hfp_mode != "off" and spec.lower().startswith("usb"):
        libusb = sonatina_hfp.use_bundled_libusb()
        if libusb is None and sys.platform == "win32":
            log.warning("libusb 1.0.29 not found: headset microphone off")
        else:
            sonatina_hfp.install_safe_iso()
            sonatina_hfp.tune_usb_transport()
            spec = sonatina_hfp.transport_spec(spec, int(settings.sco_alt))
    log.info("transport %s", spec)

    transport = await open_transport(spec)
    hci_source, hci_sink = transport.source, transport.sink
    try:
        apply_transport_quirks(settings.transport, hci_source)
        if "+sco=" in spec:
            host.hfp.lane = sonatina_hfp.ScoOutLane.install(hci_sink)
            host.hfp.usb_alt = sonatina_hfp.UsbScoAlt.install(transport)
        config = DeviceConfiguration()
        config.name = "Sonatina"
        config.class_of_device = DeviceClass.pack_class_of_device(
            DeviceClass.AUDIO_SERVICE_CLASS,
            DeviceClass.AUDIO_VIDEO_MAJOR_DEVICE_CLASS,
            DeviceClass.AUDIO_VIDEO_UNCATEGORIZED_MINOR_DEVICE_CLASS,
        )
        config.keystore = "JsonKeyStore"
        config.classic_enabled = True
        config.le_enabled = False
        config.classic_sc_enabled = False
        config.classic_smp_enabled = False
        device = Device.from_config_with_hci(config, hci_source, hci_sink)
        host.attach(device)
        await device.power_on()
        await host.after_power_on()
        log.info("Sonatina on %s, paired: %s, preferred: %s", device.public_address,
                 ", ".join(host.headset_name(a) for a in sorted(host.paired)) or "none",
                 host.headset_name(settings.address))
        source.start()
        tasks = [asyncio.create_task(host.keep_connected()),
                 asyncio.create_task(host.output.run()),
                 asyncio.create_task(host.radio.run()),
                 asyncio.create_task(host.follow_volume()),
                 asyncio.create_task(watch_capture(source)),
                 asyncio.create_task(host.hfp.run())]
        if settings.stats:
            tasks.append(asyncio.create_task(host.print_stats(
                interval=60.0 if stats_to_log else 5.0, to_log=stats_to_log)))
        suspend = asyncio.create_task(watch_suspend())
        try:
            await asyncio.wait([asyncio.ensure_future(hci_source.terminated), suspend],
                               return_when=asyncio.FIRST_COMPLETED)
            if suspend.done():
                suspend.result()   # raises SystemResumed
        finally:
            suspend.cancel()
            for t in tasks:
                t.cancel()
            source.stop()
            try:
                await host.hfp.shutdown()
            except BaseException as error:
                log.debug("hfp shutdown: %r", error)
            host.hfp._stop_audio()
            host.hfp.close_com()
            if host.pump is not None:
                await host.pump.stop()
    finally:
        await close_transport(transport)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SonatinaHost: A2DP source on a dedicated USB dongle. "
                    f"Options not given come from {SETTINGS_PATH}.")
    parser.add_argument("--transport")
    parser.add_argument("--address", help="headset to connect (AA:BB:CC:DD:EE:FF)")
    parser.add_argument("--codec", choices=CODEC_CHAIN)
    parser.add_argument("--ldac-quality", choices=["hq", "sq", "mq"])
    parser.add_argument("--sbc-bitpool", type=int)
    parser.add_argument("--capture", help="capture device name (substring)")
    parser.add_argument("--volume-endpoint",
                        help="playback endpoint whose Windows volume is followed (substring)")
    parser.add_argument("--tone", action="store_true", help="1 kHz test tone instead of VB-CABLE")
    parser.add_argument("--hfp", choices=["auto", "on", "off"],
                        help="headset microphone: auto (when an app opens it), on, off")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if not args.debug:
        logging.getLogger("bumble").setLevel(logging.WARNING)

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return

    settings = load_settings(SETTINGS_PATH)
    for name, value in (("transport", args.transport), ("address", args.address),
                        ("codec", args.codec), ("ldac_quality", args.ldac_quality),
                        ("sbc_max_bitpool", args.sbc_bitpool), ("capture", args.capture),
                        ("volume_endpoint", args.volume_endpoint), ("hfp_mode", args.hfp)):
        if value is not None:
            setattr(settings, name, norm_addr(value) if name == "address" else value)
    if args.codec or args.ldac_quality:   # explicit choice wins over the per-headset one
        for entry in settings.headsets.values():
            entry.pop("codec", None)
            entry.pop("ldac_quality", None)
    settings.tone = args.tone
    while True:
        try:
            asyncio.run(amain(settings))
            return
        except SystemResumed as gap:
            log.info("PC resumed from sleep (%s): restarting", gap)
            time.sleep(2.0)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
