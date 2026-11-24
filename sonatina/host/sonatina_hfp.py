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

Split in 1.3.0 (M7): sonatina_hfp_usb (SCO over USB), sonatina_hfp_audio (voice audio,
microphone cable), sonatina_hfp_common (constants); this module keeps the AG and the
call mode switching, and re-exports the names the rest of Sonatina used from here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import sys

log = logging.getLogger("sonatina.hfp")

from sonatina_hfp_common import (  # noqa: F401 - re-exported
    AG_RECORD_HANDLE,
    H2_SYNC,
    CVSD_SETTINGS,
    MIC_DEVICES,
    MSBC_SETTINGS,
    VOICE_CVSD,
    VOICE_TRANSPARENT,
    tr,
)
from sonatina_hfp_usb import (  # noqa: F401 - re-exported
    ScoOutLane,
    UsbScoAlt,
    install_safe_iso,
    legacy_setup_command,
    msbc_transparent_parameters,
    transport_spec,
    tune_usb_transport,
    use_bundled_libusb,
)
from sonatina_hfp_audio import (  # noqa: F401 - re-exported
    MicOutput,
    MicWatcher,
    ScoAudio,
    ScoDump,
    _endpoints,
    fit_frames,
    _process_name,
    find_mic_cable,
)

# ----------------------------------------------------------------------------- HFP AG
def ag_configuration(msbc: bool, battery: bool = True):
    """battery: HF indicators (AT+BIND / AT+BIEV), the standard way headsets report their
    battery level (1.2.0)."""
    from bumble import hfp

    features = [hfp.AgFeature.CODEC_NEGOTIATION, hfp.AgFeature.ESCO_S4_SETTINGS_SUPPORTED,
                hfp.AgFeature.ENHANCED_CALL_STATUS]
    if battery:
        features.append(hfp.AgFeature.HF_INDICATORS)
    return hfp.AgConfiguration(
        supported_ag_features=features,
        supported_ag_indicators=[hfp.AgIndicatorState.call(),
                                 hfp.AgIndicatorState.callsetup(),
                                 hfp.AgIndicatorState.callheld(),
                                 hfp.AgIndicatorState.service(),
                                 hfp.AgIndicatorState.signal(),
                                 hfp.AgIndicatorState.roam(),
                                 hfp.AgIndicatorState.battchg()],
        supported_hf_indicators=[hfp.HfIndicator.BATTERY_LEVEL] if battery else [],
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

        on_battery = None                            # (percent, source), set by HfpGateway

        def _battery(self, percent: int, source: str) -> None:
            if self.on_battery is not None and 0 <= percent <= 100:
                self.on_battery(percent, source)

        def _on_xapl(self, *_args) -> None:          # Apple accessory handshake
            self.send_response("+XAPL=iPhone,2")     # 2: we take battery reports
            self.send_ok()

        def _on_iphoneaccev(self, *args) -> None:    # Apple battery: count, (key, value)...
            try:
                values = [int(a) for a in args if not isinstance(a, list)]
                pairs = dict(zip(values[1::2], values[2::2]))
                if 1 in pairs:                        # key 1: battery 0..9
                    self._battery(min(100, (pairs[1] + 1) * 10), "iphoneaccev")
            except ValueError:
                pass
            self.send_ok()

        def _on_xevent(self, *args) -> None:         # Android: BATTERY,level,levels,...
            try:
                if args and bytes(args[0]).upper() == b"BATTERY" and len(args) >= 2:
                    level = int(args[1])
                    levels = int(args[2]) if len(args) >= 3 and args[2] else 101
                    self._battery(round(level * 100 / max(1, levels - 1)), "xevent")
            except (ValueError, TypeError):
                pass
            self.send_ok()

        def _on_bldn(self) -> None:                  # redial: we place no calls
            self.send_error()

        def __getattr__(self, name: str):
            """Any other command (vendor ones like the CMF's AT+XNTSF, 2026-11-22): ERROR,
            as a phone does. Bumble leaves it unanswered and the headset waits for a reply."""
            if not name.startswith("_on_"):
                raise AttributeError(name)

            def unsupported(*_args) -> None:
                command = name[4:].upper().replace("_READ", "?").replace("_TEST", "=?")
                if command not in UNKNOWN_AT_LOGGED:
                    UNKNOWN_AT_LOGGED.add(command)
                    log.info("HFP: headset command AT+%s not supported: answered ERROR", command)
                self.send_error()
            return unsupported

    return SonatinaAg


UNKNOWN_AT_LOGGED: set[str] = set()


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
        self._com: concurrent.futures.ThreadPoolExecutor | None = None

    @property
    def com(self) -> concurrent.futures.ThreadPoolExecutor:
        """One thread for the periodic Windows audio queries (who records from the virtual
        microphone, the default output): COM calls of 10-20 ms every half second no longer
        hold up the event loop that also moves the SCO audio. Always the same thread, so
        the COM objects of MicWatcher stay in the apartment that created them."""
        if self._com is None:
            self._com = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="sonatina-com")
        return self._com

    def close_com(self) -> None:
        if self._com is not None:
            self._com.shutdown(wait=False, cancel_futures=True)
            self._com = None

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
            AG_RECORD_HANDLE, self.channel, ag_configuration(self.can_msbc(), self.s.hfp_battery))
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
        ag = make_ag_class()(dlc, ag_configuration(self.can_msbc(), self.s.hfp_battery))
        ag.on_battery = self._on_battery
        ag.on("hf_indicator", self._on_hf_indicator)
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

        ready = self.ag_ready
        if ready is None:                 # on_connected() was not called for this link
            return
        delays = [6.0, 4.0, 8.0, 15.0]
        for attempt, wait in enumerate(delays):
            if connection is not self.connection:
                return
            try:
                await asyncio.wait_for(asyncio.shield(ready), wait)
                break
            except asyncio.TimeoutError:
                pass
            if self.ready:
                break
            if self.headset_mux or self.ag is not None:
                # only one RFCOMM session per link: the headset owns it, give it time
                try:
                    await asyncio.wait_for(asyncio.shield(ready), 15.0)
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
                await asyncio.wait_for(asyncio.shield(ready), 10.0)
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

    # -- battery (1.2.0) -------------------------------------------------------
    def _on_hf_indicator(self, state) -> None:
        from bumble import hfp
        if getattr(state, "indicator", None) == hfp.HfIndicator.BATTERY_LEVEL:
            self._on_battery(int(state.current_status), "hf indicator")

    def _on_battery(self, percent: int, source: str) -> None:
        handler = getattr(self.host, "on_battery", None)
        if handler is not None:
            handler(percent, source)

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
                              lane=self.lane, settings=self.s)
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
            if self.mic_key is None and now >= next_lookup:
                next_lookup = now + 10.0
                self.mic_key = await loop.run_in_executor(self.com, find_mic_cable, self.s.mic_device) \
                    if sys.platform == "win32" else (self.s.mic_device or None)
                if self.mic_key:
                    log.info("virtual microphone: %s", self.mic_key)
                    self.watcher = self.make_watcher(self.mic_key)
                elif not getattr(self, "_warned_cable", False):
                    self._warned_cable = True
                    log.warning("no virtual microphone (install Hi-Fi Cable): the headset "
                                "microphone cannot reach the apps")
            try:
                wanted = await self._wanted(now)
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

    async def _wanted(self, now: float) -> bool:
        if self.s.hfp_mode == "on":
            return not self.dismissed
        if self.watcher is None:
            return False
        apps = await asyncio.get_running_loop().run_in_executor(self.com, self.watcher.poll)
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
