"""Windows media sessions (Spotify, browsers, Media Player...) for the headset buttons.

Uses GlobalSystemMediaTransportControls (the API behind the media overlay of Windows):
explicit play / pause / next / previous on the current session and its real playback
status, instead of guessing from the audio. Runs in its own thread (WinRT, MTA), so
nothing here can block the Bluetooth host. Without PyWinRT, `available` stays False and
the caller falls back to media keys.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading

log = logging.getLogger("sonatina")

COMMANDS = ("play", "pause", "toggle_play_pause", "skip_next", "skip_previous", "stop")
# GlobalSystemMediaTransportControlsSessionPlaybackStatus
_STATUS = {0: "stopped", 1: "stopped", 3: "stopped", 4: "playing", 5: "paused"}  # 2 = changing
TIMEOUT_S = 2.0


class MediaSessions:
    def __init__(self) -> None:
        self.available = False
        self.status: str | None = None   # playing | paused | stopped, None = no session
        self.app: str | None = None
        self._queue: queue.Queue = queue.Queue()
        if sys.platform == "win32":
            threading.Thread(target=self._run, name="sonatina-media", daemon=True).start()

    def command(self, name: str) -> bool:
        """Queues a command for the current session. False if sessions are unavailable."""
        assert name in COMMANDS, name
        if not self.available:
            return False
        self._queue.put(name)
        return True

    # -- worker thread -----------------------------------------------------------
    def _run(self) -> None:
        try:
            import winrt.runtime
            from winrt.windows.foundation import AsyncStatus
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
            )

            try:
                winrt.runtime.init_apartment(winrt.runtime.MTA)
            except Exception:
                pass   # already initialised
            manager = Manager.request_async().get()
        except Exception as error:
            log.info("media sessions unavailable (%r): headset buttons use media keys", error)
            return
        self.available = True
        log.info("media sessions: ready")

        def wait(operation):
            if operation.wait(TIMEOUT_S) == AsyncStatus.COMPLETED:
                return operation.get_results()
            return None

        while True:
            try:
                name = self._queue.get(timeout=0.3)
            except queue.Empty:
                name = None
            try:
                session = manager.get_current_session()
                if name is not None:
                    if session is None:
                        log.info("media: no player to send %s to", name)
                    else:
                        ok = wait(getattr(session, f"try_{name}_async")())
                        log.info("media: %s -> %s (%s)", name, session.source_app_user_model_id,
                                 "ok" if ok else "refused")
                self._update(session)
            except Exception as error:
                log.debug("media sessions: %r", error)

    def _update(self, session) -> None:
        if session is None:
            self.status, self.app = None, None
            return
        self.app = session.source_app_user_model_id
        value = int(session.get_playback_info().playback_status)
        if value in _STATUS:   # keep the last status while "changing" (track change)
            self.status = _STATUS[value]


_instance: MediaSessions | None = None


def sessions() -> MediaSessions:
    """One per process (the host object is recreated when the dongle comes back)."""
    global _instance
    if _instance is None:
        _instance = MediaSessions()
    return _instance
