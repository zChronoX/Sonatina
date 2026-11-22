"""Automatic Windows output (1.1.0, A1).

Windows plays to VB-CABLE ("Sonatina Audio") and Sonatina forwards it to the headphones.
With the headphones off nobody hears that cable, so:
  - headphones ready (A2DP streaming or a call): the default output becomes the cable;
  - headphones gone for GRACE_S (or not there STARTUP_GRACE_S after start): the default
    output becomes the chosen one ("fallback": a device picked by the user, or the last
    real output used before Sonatina), and the player is paused right at the disconnection
    so the music does not burst out of the speakers;
  - Sonatina quits or the adapter is unplugged: same as headphones gone.
Only a default that is still the cable is moved away: a device the user picked by hand
stays. Disconnections Sonatina makes itself (codec change, options, switching headsets)
neither pause nor switch unless the headphones fail to come back.

All Windows calls (pycaw / COM) go through `backend` in the COM thread (HfpGateway.com);
the logic here is plain Python, driven by the event loop and testable without Windows.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sys
import time

log = logging.getLogger("sonatina")

VIRTUAL_MARKERS = ("vb-audio", "virtual cable", "hi-fi cable", "cable-a", "cable-b",
                   "steam streaming", "voicemeeter", "sonatina")
MIC_MARKERS = ("cable-a", "hi-fi cable", "steam streaming microphone")


@dataclasses.dataclass(frozen=True)
class Endpoint:
    id: str
    name: str


def is_virtual(name: str) -> bool:
    n = name.lower()
    return any(m in n for m in VIRTUAL_MARKERS)


def is_mic_cable(name: str) -> bool:
    n = name.lower()
    return any(m in n for m in MIC_MARKERS)


class WindowsBackend:
    """Active playback endpoints and the default one, through pycaw. COM thread only."""

    def __init__(self) -> None:
        self._inited = False

    def _init(self) -> None:
        if not self._inited:
            from sonatina_common import com_init
            com_init()
            self._inited = True

    def list(self) -> tuple[list[Endpoint], str | None]:
        self._init()
        from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
        from pycaw.pycaw import AudioUtilities

        enumerator = AudioUtilities.GetDeviceEnumerator()
        try:
            default = enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender.value,
                                                         ERole.eMultimedia.value).GetId()
        except Exception:
            default = None
        found = []
        endpoints = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
        for i in range(endpoints.GetCount()):
            dev = endpoints.Item(i)
            found.append(Endpoint(dev.GetId(), AudioUtilities.CreateDevice(dev).FriendlyName or ""))
        return found, default

    def set_default(self, endpoint_id: str) -> None:
        self._init()
        from pycaw.constants import ERole
        from pycaw.pycaw import AudioUtilities

        AudioUtilities.SetDefaultDevice(
            endpoint_id, roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])


class OutputSwitcher:
    TICK_S = 0.5
    GRACE_S = 3.0             # a dropout this short neither switches nor pauses twice
    STARTUP_GRACE_S = 15.0    # the headphones usually reconnect by themselves at start
    EXPECTED_S = 15.0         # Sonatina's own disconnections (codec change...): wait this long
    REFRESH_S = 10.0          # endpoint list for the menus, last real output, mic cable check
    AUDIO_PLAYING_S = 3.0     # non-silent audio this recent: something was playing

    def __init__(self, host, backend=None, executor=None, clock=time.monotonic) -> None:
        self.host = host
        self.s = host.s
        self.backend = backend if backend is not None else (WindowsBackend() if sys.platform == "win32" else None)
        self.executor = executor           # None: the host's COM thread
        self.clock = clock
        self.outputs: list[Endpoint] = []  # active real (non virtual) outputs, for the menus
        self.default: Endpoint | None = None
        self.switched: Endpoint | None = None   # where we sent Windows when the headphones went
        self.started = clock()
        self.was_ready = False
        self.link_up = False
        self.gone_since: float | None = None    # headphones not ready since (None: ready)
        self.expected_until = 0.0
        self.next_refresh = 0.0
        self.pending: str | None = None         # "cable" | "fallback"
        self.last_error: str | None = None
        self.paused = False

    # -- called by the host -------------------------------------------------------
    def expect_reconnect(self) -> None:
        """Sonatina is about to drop the link itself (it reconnects right away)."""
        self.expected_until = self.clock() + self.EXPECTED_S

    def ready_now(self) -> bool:
        h = self.host
        return h.connection is not None and (h.state == "streaming" or bool(getattr(h.hfp, "in_call", False)))

    def option_changed(self) -> None:
        """auto_output turned on, or a new fallback: apply at the next tick."""
        self.pending = "cable" if self.ready_now() else "fallback"

    # -- loop -----------------------------------------------------------------------
    async def run(self) -> None:
        if self.backend is None:
            return
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:      # never stop following the headphones
                log.warning("automatic output: %r", error)
                await asyncio.sleep(2.0)
            await asyncio.sleep(self.TICK_S)

    async def tick(self) -> None:
        now = self.clock()
        ready = self.ready_now()
        link_up = self.host.connection is not None
        if ready and not self.was_ready:
            self.gone_since = None
            self.pending = "cable"
            self.paused = False
            self.expected_until = 0.0     # the reconnection we were waiting for happened
        elif not link_up and self.link_up:          # the link just went away
            self.gone_since = now
            expected = now < self.expected_until
            if not expected:
                self._pause_player()
            self.pending = "fallback"
        elif not ready and self.gone_since is None and not self.was_ready and not link_up:
            self.gone_since = self.started            # never connected since the start
            self.pending = "fallback"
        self.was_ready, self.link_up = ready, link_up

        due = None
        if self.pending == "cable":
            due = "cable"
        elif self.pending == "fallback" and not link_up and self.gone_since is not None:
            wait = self.STARTUP_GRACE_S if self.gone_since == self.started else self.GRACE_S
            if now >= max(self.gone_since + wait, self.expected_until):
                due = "fallback"
        if due is not None or now >= self.next_refresh:
            self.next_refresh = now + self.REFRESH_S
            await self._apply(due)

    async def _call(self, fn, *args):
        executor = self.executor
        if executor is None:
            executor = getattr(self.host.hfp, "com", None)
        return await asyncio.get_running_loop().run_in_executor(executor, fn, *args)

    async def _apply(self, due: str | None) -> None:
        endpoints, default_id = await self._call(self.backend.list)
        by_id = {e.id: e for e in endpoints}
        self.default = by_id.get(default_id)
        self.outputs = [e for e in endpoints if not is_virtual(e.name)]
        self._remember(self.default)
        auto = bool(self.s.auto_output)
        cable = self._cable(endpoints)
        on_cable = self.default is not None and cable is not None and self._is_cable(self.default)
        on_mic = self.default is not None and is_mic_cable(self.default.name)
        if due == "cable" or (on_mic and (self.ready_now() or not auto)):
            self.pending = None
            if (auto or on_mic) and cable is not None and not on_cable:
                await self._set(cable, "headphones ready" if not on_mic else
                                f'"{self.default.name}" is the microphone cable')
            self.switched = None
            return
        if due == "fallback" or (on_mic and auto):
            self.pending = None
            if not auto or not (on_cable or on_mic):
                return       # off, or the user already picked another output
            target = self.fallback(endpoints)
            if target is None:
                log.info("headphones gone: no other output to switch to")
                return
            await self._set(target, "headphones gone")
            self.switched = target
            notice = getattr(self.host, "on_notice", None)
            if notice is not None:
                from sonatina_common import tr
                notice(tr("Cuffie spente: audio su ", "Headphones off: sound on ") + target.name
                       + (tr(". Musica in pausa.", ". Music paused.") if self.paused else "."))

    async def _set(self, target: Endpoint, why: str) -> None:
        try:
            await self._call(self.backend.set_default, target.id)
            log.info('Windows output -> "%s" (%s)', target.name, why)
            self.default = target
            self.last_error = None
        except Exception as error:
            self.last_error = repr(error)
            log.warning('cannot make "%s" the Windows output: %r', target.name, error)

    # -- choices --------------------------------------------------------------------
    def _is_cable(self, endpoint: Endpoint) -> bool:
        return is_cable(self.s, endpoint)

    def _cable(self, endpoints: list[Endpoint]) -> Endpoint | None:
        from sonatina_audio import pick_cable_output
        index = pick_cable_output([e.name for e in endpoints], self.s.volume_endpoint)
        return None if index is None else endpoints[index]

    def _remember(self, default: Endpoint | None) -> None:
        """The last real output the user had: the automatic fallback."""
        if default is None or is_virtual(default.name):
            return
        if self.switched is not None and default.id == self.switched.id:
            return   # our own switch, not a choice
        if self.s.last_output != default.id or self.s.last_output_name != default.name:
            self.s.last_output, self.s.last_output_name = default.id, default.name
            self.host._changed()

    def fallback(self, endpoints: list[Endpoint] | None = None) -> Endpoint | None:
        return choose_fallback(self.s, self.outputs if endpoints is None else endpoints)

    def _pause_player(self) -> None:
        self.paused = False
        if not self.s.pause_on_disconnect:
            return
        try:
            if self.host.audio_age() > self.AUDIO_PLAYING_S:
                return      # nothing was playing
        except Exception:
            return
        media = getattr(self.host.remote, "media", None)
        if media is not None and getattr(media, "available", False):
            if getattr(media, "status", None) not in (None, "playing"):
                return
            media.command("pause")
        elif sys.platform == "win32":
            import sonatina_avrcp
            sonatina_avrcp.send_key(sonatina_avrcp.VK_MEDIA_PLAY_PAUSE)
        else:
            return
        self.paused = True
        log.info("headphones gone while playing: player paused")

    # -- for the dashboard and the tray --------------------------------------------
    def snapshot(self) -> dict:
        fb = self.fallback()
        return {
            "auto": bool(self.s.auto_output),
            "pause": bool(self.s.pause_on_disconnect),
            "fallback_id": self.s.fallback_output,
            "fallback_name": self.s.fallback_output_name or None,
            "automatic_name": getattr(choose_fallback(self.s, self.outputs, automatic=True), "name", None),
            "target": None if fb is None else fb.name,
            "current": None if self.default is None else self.default.name,
            "on_cable": bool(self.default is not None and self._is_cable(self.default)),
            "switched": None if self.switched is None else self.switched.name,
            "outputs": [{"id": e.id, "name": e.name} for e in self.outputs],
            "error": self.last_error,
        }


def is_cable(settings, endpoint: Endpoint) -> bool:
    return settings.volume_endpoint.lower() in endpoint.name.lower()


def choose_fallback(settings, endpoints: list[Endpoint], automatic: bool = False) -> Endpoint | None:
    """The output for when the headphones are off: the one the user picked, else the last
    real output they used, else the first real one (by id, then by name: the id changes
    when a driver is reinstalled). automatic=True: what "Automatic" would pick."""
    real = [e for e in endpoints if not is_virtual(e.name)]
    wanted = [(settings.last_output, settings.last_output_name)]
    if not automatic:
        wanted.insert(0, (settings.fallback_output, settings.fallback_output_name))
    for want_id, want_name in wanted:
        if not want_id and not want_name:
            continue
        for e in real:
            if e.id == want_id:
                return e
        for e in real:
            if want_name and e.name == want_name:
                return e
    return real[0] if real else None


def leave_cable(settings, backend=None) -> str | None:
    """Sonatina quits or the adapter is gone: the default output must not stay on a cable
    nobody listens to. Synchronous (tray thread; the backend initialises COM)."""
    if not settings.auto_output or (backend is None and sys.platform != "win32"):
        return None
    backend = backend or WindowsBackend()
    endpoints, default_id = backend.list()
    default = next((e for e in endpoints if e.id == default_id), None)
    if default is None or not (is_cable(settings, default) or is_mic_cable(default.name)):
        return None
    target = choose_fallback(settings, endpoints)
    if target is None:
        return None
    backend.set_default(target.id)
    log.info('Windows output -> "%s" (Sonatina not running)', target.name)
    return target.name
