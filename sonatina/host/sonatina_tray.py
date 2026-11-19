"""SonatinaHost in the notification area (no console).

Started at logon by the shortcut that install-host.ps1 puts in the Startup folder:
    .venv\\Scripts\\pythonw.exe sonatina_tray.py
Log: %LOCALAPPDATA%\\Sonatina\\sonatina.log
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import logging.handlers
import os
import sys
import threading
import time

import sonatina_host as sh

log = logging.getLogger("sonatina")

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Sonatina")
LOG_PATH = os.path.join(APP_DIR, "sonatina.log")
RETRY_S = 5.0


def setup_logging() -> None:
    os.makedirs(APP_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s",
                                           "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("bumble").setLevel(logging.WARNING)


def single_instance() -> bool:
    """Only one process may own the dongle."""
    if sys.platform != "win32":
        return True
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\SonatinaHost")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def make_icon(color: tuple[int, int, int]):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color + (255,))
    # a simple eighth note
    d.ellipse((18, 38, 34, 50), fill=(255, 255, 255, 255))
    d.rectangle((30, 14, 34, 44), fill=(255, 255, 255, 255))
    d.polygon([(34, 14), (46, 20), (46, 26), (34, 20)], fill=(255, 255, 255, 255))
    return img


COLORS = {
    "streaming": (37, 99, 235),   # blue
    "idle": (113, 113, 122),      # grey
    "error": (220, 38, 38),       # red
}


class TrayApp:
    def __init__(self, settings: sh.Settings) -> None:
        self.settings = settings
        self.host: sh.SonatinaHost | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.main_task: asyncio.Task | None = None
        self.quitting = False
        self.dongle_ok = False
        self.icon = None

    # -- host thread ----------------------------------------------------------
    def _on_host(self, host: sh.SonatinaHost) -> None:
        self.host = host

    async def _run_once(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.main_task = asyncio.current_task()
        self.dongle_ok = True
        await sh.amain(self.settings, on_host=self._on_host, stats_to_log=True)

    def host_thread(self) -> None:
        while not self.quitting:
            try:
                asyncio.run(self._run_once())
                log.warning("dongle gone, retrying in %.0f s", RETRY_S)
            except asyncio.CancelledError:
                pass
            except Exception as error:
                log.error("host stopped: %r (retry in %.0f s)", error, RETRY_S)
            self.host = None
            self.dongle_ok = False
            if not self.quitting:
                time.sleep(RETRY_S)

    def restart(self) -> None:
        if self.loop and self.main_task:
            self.loop.call_soon_threadsafe(self.main_task.cancel)

    # -- tray -----------------------------------------------------------------
    def status(self) -> tuple[str, str]:
        if self.host is None:
            return "error", "Sonatina: chiavetta non trovata" if not self.dongle_ok else "Sonatina: avvio..."
        state = self.host.state
        key = "streaming" if state == "streaming" else ("error" if state == "error" else "idle")
        return key, self.host.summary()

    def refresher(self) -> None:
        last = None
        while not self.quitting:
            key, text = self.status()
            if self.icon is not None and (key, text) != last:
                last = (key, text)
                self.icon.icon = make_icon(COLORS[key])
                self.icon.title = text[:127]
            time.sleep(1.0)

    def open_log(self, *_args) -> None:
        if sys.platform == "win32":
            os.startfile(LOG_PATH)

    def on_restart(self, *_args) -> None:
        log.info("restart requested from the tray")
        self.restart()

    def on_quit(self, icon, *_args) -> None:
        self.quitting = True
        self.restart()
        icon.stop()

    def run(self) -> None:
        import pystray

        threading.Thread(target=self.host_thread, name="sonatina-host", daemon=True).start()
        threading.Thread(target=self.refresher, name="sonatina-tray", daemon=True).start()
        menu = pystray.Menu(
            pystray.MenuItem(lambda _item: self.status()[1], None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Riavvia connessione", self.on_restart),
            pystray.MenuItem("Apri log", self.open_log),
            pystray.MenuItem("Esci", self.on_quit),
        )
        self.icon = pystray.Icon("Sonatina", make_icon(COLORS["idle"]), "Sonatina", menu)
        self.icon.run()


def main() -> None:
    setup_logging()
    if not single_instance():
        log.info("another SonatinaHost is already running")
        return
    log.info("SonatinaHost tray starting")
    TrayApp(sh.Settings(stats=True)).run()
    log.info("SonatinaHost tray stopped")
    os._exit(0)  # do not wait for the USB thread


if __name__ == "__main__":
    main()
