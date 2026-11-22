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
import sonatina_ipc

log = logging.getLogger("sonatina")

APP_DIR = sh.APP_DIR
LOG_PATH = os.path.join(APP_DIR, "sonatina.log")
tr = sh.tr
CODEC_CHOICES = [  # (label, codec, ldac quality)
    (tr("LDAC 990 kbit/s (massima qualita')", "LDAC 990 kbit/s (best quality)"), "ldac", "hq"),
    ("LDAC 660 kbit/s", "ldac", "sq"),
    (tr("LDAC 330 kbit/s (connessione debole)", "LDAC 330 kbit/s (weak link)"), "ldac", "mq"),
    ("aptX HD 576 kbit/s", "aptx_hd", None),
    ("aptX 384 kbit/s", "aptx", None),
    (tr("AAC (cuffie senza LDAC)", "AAC (headphones without LDAC)"), "aac", None),
    (tr("SBC (compatibile con tutte)", "SBC (works with all headphones)"), "sbc", None),
]
RETRY_S = 5.0
OPTION_NAMES = ("absolute_volume", "low_latency", "media_keys", "hfp_msbc", "ldac_adaptive")
STARTUP_LINK = "Sonatina.lnk"


def dashboard_path() -> str | None:
    """SonatinaDashboard.exe: next to Sonatina.exe when installed, the dotnet publish output
    in a checkout, or SONATINA_DASHBOARD."""
    candidates = [os.environ.get("SONATINA_DASHBOARD", "")]
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), "dashboard", "SonatinaDashboard.exe"))
    else:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard", "publish")
        candidates.append(os.path.join(root, "SonatinaDashboard.exe"))
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return None


def launch_dashboard() -> bool:
    path = dashboard_path()
    if path is None:
        log.warning("dashboard not found (not built or not installed)")
        return False
    import subprocess
    try:
        subprocess.Popen([path], cwd=os.path.dirname(path), close_fds=True)
        return True
    except Exception as error:
        log.error("cannot start the dashboard: %r", error)
        return False


def startup_folder() -> str:
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def autostart_enabled() -> bool:
    return os.path.isfile(os.path.join(startup_folder(), STARTUP_LINK))


def set_autostart(on: bool) -> None:
    """The same shortcut the installer (and install-host.ps1) puts in the Startup folder."""
    link = os.path.join(startup_folder(), STARTUP_LINK)
    if not on:
        if os.path.isfile(link):
            os.remove(link)
        return
    sh.com_init()
    import comtypes.client
    shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)
    shortcut = shell.CreateShortcut(link)
    if getattr(sys, "frozen", False):
        shortcut.TargetPath = sys.executable
        shortcut.WorkingDirectory = os.path.dirname(sys.executable)
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        shortcut.TargetPath = pythonw if os.path.isfile(pythonw) else sys.executable
        shortcut.Arguments = '"' + os.path.abspath(__file__) + '"'
        shortcut.WorkingDirectory = os.path.dirname(os.path.abspath(__file__))
    shortcut.Save()


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


_MUTEX = None


def single_instance() -> bool:
    """Only one process may own the dongle. A failed attempt closes its handle, so that
    a relaunched process can take the mutex once the old one has exited."""
    global _MUTEX
    if sys.platform != "win32":
        return True
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k32.CreateMutexW(None, False, "Local\\SonatinaHost")
    if ctypes.windll.kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
        if handle:
            k32.CloseHandle(handle)
        return False
    _MUTEX = handle
    return True


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
    "call": (22, 163, 74),        # green: headset microphone live
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
        self.first_run_search = False
        self.access_errors = 0             # LIBUSB_ERROR_ACCESS in a row
        self.dongle_ever = False           # this process has had the adapter open

    # -- host thread ----------------------------------------------------------
    def _on_host(self, host: sh.SonatinaHost) -> None:
        host.on_change = lambda settings: sh.save_settings(settings, sh.SETTINGS_PATH)
        self.host = host
        self._warned_missing = False   # the adapter is back

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
        sh.com_init()   # pycaw (volume) runs on this thread
        while not self.quitting:
            try:
                asyncio.run(self._run_once())
                if not sh.ADAPTER_STUCK:
                    log.warning("dongle gone, retrying in %.0f s", RETRY_S)
            except asyncio.CancelledError:
                pass
            except sh.SystemResumed as gap:
                log.info("PC resumed from sleep (%s): restarting Bluetooth", gap)
            except Exception as error:
                if "Access" in type(error).__name__ or "ERROR_ACCESS" in repr(error):
                    self.access_errors += 1
                    log.error("adapter busy (%r), attempt %d", error, self.access_errors)
                    if self.access_errors >= 3 and self.dongle_ever and not self.quitting:
                        self.relaunch()     # the stale handle is ours: only a new process frees it
                elif "device not found" in repr(error):   # adapter unplugged: wait quietly
                    if not getattr(self, "_warned_missing", False):
                        log.warning("Bluetooth adapter not found, waiting for it")
                        self._warned_missing = True
                else:
                    log.error("host stopped: %r (retry in %.0f s)", error, RETRY_S)
            self.host = None
            self.dongle_ok = False
            if sh.ADAPTER_STUCK and not self.quitting:
                self.relaunch()      # the adapter did not close: a new process frees it
            if not self.quitting:
                time.sleep(RETRY_S)

    # -- dashboard channel (sonatina_ipc) ----------------------------------------
    def snapshot(self) -> dict:
        host = self.host
        key, text = self.status()
        return {
            "app": {"version": sh.app_version(), "key": key, "tooltip": text,
                    "adapter": self.dongle_ok, "host": host is not None,
                    "autostart": autostart_enabled(), "log": LOG_PATH,
                    "italian": sh.UI_ITALIAN},
            "host": host.snapshot() if host is not None else None,
        }

    async def _on_host_loop(self, coro):
        """Runs a host coroutine on the host thread and waits for it from another loop."""
        loop = self.loop
        if loop is None or loop.is_closed() or self.host is None:
            coro.close()
            raise RuntimeError("Bluetooth not running")
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    async def command(self, name: str, args: dict):
        host = self.host
        if name == "open_log":
            if sys.platform == "win32":
                os.startfile(LOG_PATH)
            return True
        if name == "restart":
            self.restart()
            return True
        if name == "set_autostart":
            set_autostart(bool(args.get("value")))
            return autostart_enabled()
        if name == "set_hfp_mode":
            mode = str(args.get("mode"))
            if mode not in ("auto", "on", "off"):
                raise ValueError("mode")
            if host is None:
                self.settings.hfp_mode = mode
                sh.save_settings(self.settings, sh.SETTINGS_PATH)
                return "saved"
            if await self._on_host_loop(host.set_hfp_mode(mode)):
                log.info("headset microphone %s (dashboard): restarting Sonatina", mode)
                threading.Thread(target=self.relaunch, daemon=True).start()
                return "restarting"
            return "ok"
        if host is None:
            raise RuntimeError("Bluetooth not running")
        if name == "select":
            await self._on_host_loop(host.select(str(args["address"])))
        elif name == "set_codec":
            await self._on_host_loop(host.set_codec(str(args["address"]), str(args["codec"]),
                                                    args.get("ldac_quality")))
        elif name == "set_option":
            option = str(args.get("name"))
            if option not in OPTION_NAMES:
                raise ValueError("option")
            await self._on_host_loop(host.set_option(option, bool(args.get("value"))))
        elif name == "set_volume":
            percent = max(0, min(100, int(args.get("percent", 0))))
            host.volume.set_scalar(percent / 100)
        elif name == "end_call":
            async def end():
                if host.hfp.in_call:
                    host.hfp.dismissed = True
                    await host.hfp.leave_call()
            await self._on_host_loop(end())
        elif name == "discover":
            found = await self._on_host_loop(host.discover(float(args.get("seconds", 12))))
            return [{"address": a, "name": n} for a, n in sorted(found.items()) if a not in host.paired]
        elif name == "pair":
            return await self._on_host_loop(host.pair(str(args["address"]), args.get("name")))
        elif name == "forget":
            await self._on_host_loop(host.forget(str(args["address"])))
        else:
            raise ValueError(f"unknown command {name}")
        self._refresh_menu()
        return True

    def open_dashboard(self, *_args) -> None:
        if not launch_dashboard():
            self.notify(tr("La dashboard non e' installata in questa copia di Sonatina.",
                           "The dashboard is not installed in this copy of Sonatina."))

    def restart(self) -> None:
        """Cancels the host task once (a second click while it closes is ignored)."""
        loop, task = self.loop, self.main_task
        if loop is None or task is None or loop.is_closed() or task.done():
            return

        def cancel():
            if not task.done() and not task.cancelling():
                task.cancel()
        try:
            loop.call_soon_threadsafe(cancel)
        except RuntimeError:     # the loop closed meanwhile
            pass

    def relaunch(self) -> None:
        """Starts a fresh Sonatina and exits this one (frees WinUSB for sure)."""
        import subprocess
        log.warning("the adapter is still held by this process: restarting Sonatina")
        if getattr(sys, "frozen", False):
            args = [sys.executable, "--relaunched"]
        else:
            args = [sys.executable, os.path.abspath(sys.argv[0]), "--relaunched"]
        try:
            subprocess.Popen(args, cwd=os.path.dirname(os.path.abspath(sys.argv[0])), close_fds=True,
                             creationflags=0x00000008 | 0x00000200)   # detached, new group
        except Exception as error:
            log.error("cannot restart Sonatina: %r", error)
            return
        self.quitting = True
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass
        os._exit(0)

    # -- tray -----------------------------------------------------------------
    def status(self) -> tuple[str, str]:
        if self.host is None:
            return "error", (tr("Sonatina: chiavetta non trovata", "Sonatina: adapter not found")
                             if not self.dongle_ok else tr("Sonatina: avvio...", "Sonatina: starting..."))
        state = self.host.state
        key = "streaming" if state == "streaming" else ("error" if state == "error" else "idle")
        if self.host.hfp.in_call:
            key = "call"
        return key, self.host.summary()

    def refresher(self) -> None:
        last = None
        while not self.quitting:
            host = self.host
            if host is not None and getattr(host, "ready", False):
                self.dongle_ever = True
                self.access_errors = 0
            if (not self.first_run_search and host is not None and host.state == "idle"
                    and getattr(host, "ready", False) and not host.paired
                    and self.loop is not None):
                self.first_run_search = True   # no headset yet: start pairing right away
                time.sleep(2)
                self.on_search()
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
        self.notify(tr("Metti le cuffie in modalita' abbinamento: ricerca per 12 secondi...",
                       "Put the headphones in pairing mode: searching for 12 seconds..."))

        async def search():
            found = await host.discover(12)
            new = {a: n for a, n in found.items() if a not in host.paired}
            self.found = new
            if new:
                self.notify(tr("Trovate: ", "Found: ") + ", ".join(new.values()) +
                            tr(". Scegli in Cuffie > Abbina.", ". Choose Headphones > Pair."))
            else:
                self.notify(tr("Nessuna cuffia nuova trovata. Sono in modalita' abbinamento?",
                               "No new headphones found. Are they in pairing mode?"))

        self.run_coro(search())

    def _pair_action(self, address: str, name: str):
        def action(*_args):
            host = self.host
            if host is None:
                return

            async def pair():
                ok = await host.pair(address, name)
                self.found.pop(address, None)
                self.notify(f"{name}: " + (tr("abbinate", "paired") if ok
                                            else tr("abbinamento non riuscito", "pairing failed")))

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
            yield pystray.MenuItem(tr("Chiavetta non pronta", "Adapter not ready"), None, enabled=False)
            return
        for address in sorted(host.paired):
            yield pystray.MenuItem(
                host.headset_name(address), self._select_action(address),
                checked=lambda _i, a=address: self.host is not None and self.host.s.address == a,
                radio=True)
        if not host.paired:
            yield pystray.MenuItem(tr("Nessuna cuffia abbinata", "No paired headphones"), None, enabled=False)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(tr("Cerca nuove cuffie...", "Search for new headphones..."), self.on_search,
                               enabled=not host.discovering)
        for address, name in sorted(self.found.items(), key=lambda kv: kv[1]):
            yield pystray.MenuItem(tr("Abbina: ", "Pair: ") + name, self._pair_action(address, name))
        if host.s.address:
            yield pystray.Menu.SEPARATOR
            yield pystray.MenuItem(tr("Dimentica ", "Forget ") + host.headset_name(host.s.address),
                                   self._forget_action(host.s.address))

    def codec_menu(self):
        import pystray

        for label, codec, quality in CODEC_CHOICES:
            yield pystray.MenuItem(label, self._codec_action(codec, quality),
                                   checked=self._codec_checked(codec, quality), radio=True)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(tr("LDAC adattivo (scende se la radio non regge)",
                                  "Adaptive LDAC (lower when the radio struggles)"),
                               self._option_action("ldac_adaptive"),
                               checked=self._option_checked("ldac_adaptive"))

    def _option_action(self, name: str):
        def action(*_args):
            host = self.host
            if host is not None:
                self.run_coro(host.set_option(name, not getattr(host.s, name)))
        return action

    def _option_checked(self, name: str):
        return lambda _item: self.host is not None and bool(getattr(self.host.s, name, False))

    # -- headset microphone ---------------------------------------------------
    def _hfp_mode_action(self, mode: str):
        def action(*_args):
            host = self.host
            if host is None:
                self.settings.hfp_mode = mode
                sh.save_settings(self.settings, sh.SETTINGS_PATH)
                return

            async def apply():
                if await host.set_hfp_mode(mode):
                    # the USB transport changes (SCO endpoints on/off): a fresh process
                    # is the clean way to reopen the adapter
                    log.info("headset microphone %s: restarting Sonatina", mode)
                    threading.Thread(target=self.relaunch, daemon=True).start()
            self.run_coro(apply())
        return action

    def _hfp_mode_checked(self, mode: str):
        def checked(_item):
            s = self.host.s if self.host is not None else self.settings
            return s.hfp_mode == mode
        return checked

    def mic_menu(self):
        import pystray

        for label, mode in (
                (tr("Automatico (quando un'app usa il microfono)",
                    "Automatic (when an app uses the microphone)"), "auto"),
                (tr("Sempre attivo (modalita' chiamata)", "Always on (call mode)"), "on"),
                (tr("Disattivato", "Off"), "off")):
            yield pystray.MenuItem(label, self._hfp_mode_action(mode),
                                   checked=self._hfp_mode_checked(mode), radio=True)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(tr("Voce in alta qualita' (mSBC 16 kHz)", "HD voice (mSBC 16 kHz)"),
                               self._option_action("hfp_msbc"), checked=self._option_checked("hfp_msbc"))
        host = self.host
        if host is not None:
            yield pystray.Menu.SEPARATOR
            yield pystray.MenuItem(host.hfp.status_line(), None, enabled=False)

    def open_log(self, *_args) -> None:
        if sys.platform == "win32":
            os.startfile(LOG_PATH)

    def on_restart(self, *_args) -> None:
        log.info("restart requested from the tray")
        self.restart()

    def on_quit(self, icon, *_args) -> None:
        self.quitting = True
        try:
            self.restart()
        except Exception as error:
            log.debug("quit: %r", error)
        icon.stop()

    def run(self) -> None:
        import pystray

        threading.Thread(target=self.host_thread, name="sonatina-host", daemon=True).start()
        threading.Thread(target=self.refresher, name="sonatina-tray", daemon=True).start()
        try:
            self.ipc = sonatina_ipc.IpcServer(self, sh.IPC_PATH, sh.app_version())
            self.ipc.start_thread()
        except Exception as error:
            log.error("dashboard channel not started: %r", error)
        menu = pystray.Menu(
            pystray.MenuItem(lambda _item: self.status()[1], None, enabled=False),
            pystray.MenuItem(tr("Apri Sonatina", "Open Sonatina"), self.open_dashboard, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(tr("Cuffie", "Headphones"), pystray.Menu(self.headsets_menu)),
            pystray.MenuItem(tr("Qualita' audio", "Audio quality"), pystray.Menu(self.codec_menu)),
            pystray.MenuItem(tr("Microfono delle cuffie", "Headphone microphone"), pystray.Menu(self.mic_menu)),
            pystray.MenuItem(tr("Volume regolato dalle cuffie", "Volume handled by the headphones"),
                             self._option_action("absolute_volume"),
                             checked=self._option_checked("absolute_volume")),
            pystray.MenuItem(tr("Bassa latenza (giochi, video)", "Low latency (games, video)"),
                             self._option_action("low_latency"),
                             checked=self._option_checked("low_latency")),
            pystray.MenuItem(tr("Tasti delle cuffie (play/pausa, brani)",
                                "Headphone buttons (play/pause, tracks)"),
                             self._option_action("media_keys"),
                             checked=self._option_checked("media_keys")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(tr("Riavvia connessione", "Restart connection"), self.on_restart),
            pystray.MenuItem(tr("Apri log", "Open log"), self.open_log),
            pystray.MenuItem(tr("Esci", "Exit"), self.on_quit),
        )
        self.icon = pystray.Icon("Sonatina", make_icon(COLORS["idle"]), "Sonatina", menu)
        self.icon.run()


def main() -> None:
    setup_logging()
    if getattr(sys, "frozen", False):
        # Installed build: the Realtek firmware ships next to Sonatina.exe.
        firmware = os.path.join(os.path.dirname(sys.executable), "firmware")
        if os.path.isdir(firmware):
            os.environ.setdefault("BUMBLE_RTK_FIRMWARE_DIR", firmware)
    if "--set-default-output" in sys.argv:   # used once by the installer
        try:
            log.info("default output: %s", sh.set_default_output())
        except Exception as error:
            log.error("cannot set the default output: %r", error)
        return
    ok = single_instance()
    if not ok and "--relaunched" in sys.argv:    # the old process is still exiting
        for _ in range(30):
            time.sleep(0.5)
            ok = single_instance()
            if ok:
                break
    if not ok:
        log.info("another SonatinaHost is already running: opening the dashboard")
        if "--relaunched" not in sys.argv:
            launch_dashboard()    # started again from the Start menu: show the window
        return
    log.info("SonatinaHost tray starting")
    settings = sh.load_settings(sh.SETTINGS_PATH)
    if not settings.audio_configured:   # first run: Windows plays to VB-CABLE
        try:
            log.info("default output: %s", sh.set_default_output())
            settings.audio_configured = True
            sh.save_settings(settings, sh.SETTINGS_PATH)
        except Exception as error:
            log.warning("cannot set the default output yet: %r", error)
    try:
        sh.fix_default_output()
    except Exception as error:
        log.warning("cannot check the default output: %r", error)
    TrayApp(settings).run()
    log.info("SonatinaHost tray stopped")
    os._exit(0)  # do not wait for the USB thread


if __name__ == "__main__":
    main()
