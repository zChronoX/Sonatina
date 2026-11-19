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

APP_DIR = sh.APP_DIR
LOG_PATH = os.path.join(APP_DIR, "sonatina.log")
CODEC_CHOICES = [  # (label, codec, ldac quality)
    ("LDAC 990 kbit/s (massima qualita')", "ldac", "hq"),
    ("LDAC 660 kbit/s", "ldac", "sq"),
    ("LDAC 330 kbit/s (connessione debole)", "ldac", "mq"),
    ("SBC (compatibile con tutte)", "sbc", None),
]
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
        self.found: dict[str, str] = {}   # discovered, not yet paired

    # -- host thread ----------------------------------------------------------
    def _on_host(self, host: sh.SonatinaHost) -> None:
        host.on_change = lambda settings: sh.save_settings(settings, sh.SETTINGS_PATH)
        self.host = host

    def run_coro(self, coro) -> None:
        """Runs a host coroutine from the tray thread."""
        if self.loop is None or self.host is None:
            coro.close()
            return
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        future.add_done_callback(lambda f: self._refresh_menu())

    def notify(self, text: str) -> None:
        try:
            if self.icon is not None:
                self.icon.notify(text, "Sonatina")
        except Exception:
            pass

    def _refresh_menu(self) -> None:
        try:
            if self.icon is not None:
                self.icon.update_menu()
        except Exception:
            pass

    async def _run_once(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.main_task = asyncio.current_task()
        self.dongle_ok = True
        self.settings = sh.load_settings(sh.SETTINGS_PATH)  # re-read: may have changed
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
                self._refresh_menu()
            time.sleep(1.0)

    # -- headsets menu --------------------------------------------------------
    def on_search(self, *_args) -> None:
        host = self.host
        if host is None or host.discovering:
            return
        self.notify("Metti le cuffie in modalita' abbinamento: ricerca per 12 secondi...")

        async def search():
            found = await host.discover(12)
            new = {a: n for a, n in found.items() if a not in host.paired}
            self.found = new
            if new:
                self.notify("Trovate: " + ", ".join(new.values()) + ". Scegli in Cuffie > Abbina.")
            else:
                self.notify("Nessuna cuffia nuova trovata. Sono in modalita' abbinamento?")

        self.run_coro(search())

    def _pair_action(self, address: str, name: str):
        def action(*_args):
            host = self.host
            if host is None:
                return

            async def pair():
                ok = await host.pair(address, name)
                self.found.pop(address, None)
                self.notify(f"{name}: abbinate" if ok else f"{name}: abbinamento non riuscito")

            self.run_coro(pair())
        return action

    def _select_action(self, address: str):
        return lambda *_a: self.host and self.run_coro(self.host.select(address))

    def _forget_action(self, address: str):
        return lambda *_a: self.host and self.run_coro(self.host.forget(address))

    def _codec_action(self, codec: str, quality):
        def action(*_args):
            host = self.host
            if host is not None and host.s.address:
                self.run_coro(host.set_codec(host.s.address, codec, quality))
        return action

    def _codec_checked(self, codec: str, quality):
        def checked(_item):
            host = self.host
            if host is None or not host.s.address:
                return False
            c, q = host.codec_for(host.s.address)
            return c == codec and (codec != "ldac" or q == quality)
        return checked

    def headsets_menu(self):
        import pystray

        host = self.host
        if host is None:
            yield pystray.MenuItem("Chiavetta non pronta", None, enabled=False)
            return
        for address in sorted(host.paired):
            yield pystray.MenuItem(
                host.headset_name(address), self._select_action(address),
                checked=lambda _i, a=address: self.host is not None and self.host.s.address == a,
                radio=True)
        if not host.paired:
            yield pystray.MenuItem("Nessuna cuffia abbinata", None, enabled=False)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Cerca nuove cuffie...", self.on_search, enabled=not host.discovering)
        for address, name in sorted(self.found.items(), key=lambda kv: kv[1]):
            yield pystray.MenuItem(f"Abbina: {name}", self._pair_action(address, name))
        if host.s.address:
            yield pystray.Menu.SEPARATOR
            yield pystray.MenuItem(f"Dimentica {host.headset_name(host.s.address)}",
                                   self._forget_action(host.s.address))

    def codec_menu(self):
        import pystray

        for label, codec, quality in CODEC_CHOICES:
            yield pystray.MenuItem(label, self._codec_action(codec, quality),
                                   checked=self._codec_checked(codec, quality), radio=True)

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
            pystray.MenuItem("Cuffie", pystray.Menu(self.headsets_menu)),
            pystray.MenuItem("Qualita' audio", pystray.Menu(self.codec_menu)),
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
    TrayApp(sh.load_settings(sh.SETTINGS_PATH)).run()
    log.info("SonatinaHost tray stopped")
    os._exit(0)  # do not wait for the USB thread


if __name__ == "__main__":
    main()
