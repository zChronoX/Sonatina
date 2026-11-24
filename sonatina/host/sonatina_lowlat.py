"""Low latency, live and per app (M3, 1.3.0).

Low latency is a smaller jitter buffer (8 ms of margin instead of 20): sound arrives about
12 ms sooner, with a little more risk of gaps when Windows is late. Until 1.2 it needed a
reconnection; now the media clock servo just steers the buffer to the new target in a few
seconds, without a gap.

Three modes: off, always on, or only while one of the chosen apps is open. "Open" means
the app has an audio session on the cable Sonatina plays from (Windows' own list, the one
the volume mixer shows): games and players open it at start, even before they make a
sound. The same list offers the apps to choose from. When the last chosen app closes,
low latency stays on 10 more seconds (a game restarting, a player between files).
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

log = logging.getLogger("sonatina")

MODES = ("off", "on", "apps")
IGNORED_APPS = ("system", "audiodg.exe", "systemsettings.exe", "sonatina.exe", "python.exe",
                "pythonw.exe", "explorer.exe", "shellexperiencehost.exe")


def audio_apps(match: str = "VB-Audio Virtual Cable") -> list[str]:
    """Processes with an audio session (active or idle) on the cable. COM thread only."""
    if sys.platform != "win32":
        return []
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioSessionControl2, IAudioSessionManager2

    from sonatina_audio import pick_cable_output
    from sonatina_hfp_audio import _endpoints, _process_name

    endpoints = _endpoints("render")
    index = pick_cable_output([name for name, _dev in endpoints], match)
    if index is None:
        return []
    mgr = endpoints[index][1].Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
    mgr = mgr.QueryInterface(IAudioSessionManager2)
    sessions = mgr.GetSessionEnumerator()
    names = set()
    for i in range(sessions.GetCount()):
        ctl = sessions.GetSession(i)
        if ctl is None or ctl.GetState() == 2:           # AudioSessionStateExpired
            continue
        try:
            pid = ctl.QueryInterface(IAudioSessionControl2).GetProcessId()
        except Exception:
            continue
        name = _process_name(pid)
        if name.lower() not in IGNORED_APPS and not name.startswith("pid "):
            names.add(name)
    return sorted(names, key=str.lower)


def effective_mode(settings) -> str:
    mode = getattr(settings, "low_latency_mode", "") or ""
    if mode in MODES:
        return mode
    return "on" if settings.low_latency else "off"      # settings saved by 1.2


class LowLatency:
    POLL_S = 3.0
    HOLD_S = 10.0

    def __init__(self, host, lister=None, clock=time.monotonic) -> None:
        self.host = host
        self.s = host.s
        self.lister = lister            # None: audio_apps on the host's COM thread
        self.clock = clock
        self.running: list[str] = []    # apps with audio open now (the dashboard's choices)
        self.because: str | None = None
        self.last_seen = -1e9           # a chosen app was open
        self.active = effective_mode(self.s) == "on"
        self.error_logged = False

    @property
    def mode(self) -> str:
        return effective_mode(self.s)

    def chosen(self) -> list[str]:
        return list(getattr(self.s, "low_latency_apps", []) or [])

    async def _list(self) -> list[str]:
        if self.lister is not None:
            return list(self.lister())
        executor = getattr(self.host.hfp, "com", None)
        return await asyncio.get_running_loop().run_in_executor(
            executor, audio_apps, self.s.volume_endpoint)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not self.error_logged:
                    self.error_logged = True
                    log.warning("low latency: cannot list the apps (%r)", error)
            await asyncio.sleep(self.POLL_S)

    async def tick(self) -> None:
        self.running = await self._list()
        self.error_logged = False
        await self.update()

    async def update(self) -> None:
        """Decides from the last app list and the settings (also right after a change)."""
        mode, now = self.mode, self.clock()
        wanted = {a.lower() for a in self.chosen()}
        open_now = [a for a in self.running if a.lower() in wanted]
        if open_now:
            self.last_seen = now
        if mode == "on":
            want, self.because = True, None
        elif mode == "apps" and open_now:
            want, self.because = True, open_now[0]
        elif mode == "apps" and self.active and now - self.last_seen < self.HOLD_S:
            want = True                                 # just closed: wait a little
        else:
            want, self.because = False, None
        if want != self.active:
            self.active = want
            why = {"on": "always on", "off": "off"}.get(mode) or (
                f"{self.because} is open" if want else "no chosen app open")
            log.info("low latency %s (%s)", "on" if want else "off", why)
            self.host.apply_low_latency()

    def snapshot(self) -> dict:
        return {"mode": self.mode, "apps": self.chosen(), "running": self.running,
                "active": self.active, "because": self.because if self.active and self.mode == "apps" else None}
