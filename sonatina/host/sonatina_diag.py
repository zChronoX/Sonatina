"""Export diagnostics (1.1.0, A6): one zip with everything needed to understand a problem.

Contents: the log and its rotated copies, the dashboard log, settings.json, the full
status the window shows (status.json) and info.txt (version, Windows, audio devices).
Never the pairing keys (Bumble keeps them elsewhere and they are not listed here).
Saved on the Desktop as Sonatina-diagnostica-YYYYMMDD-HHMMSS.zip.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import sys
import time
import zipfile

from sonatina_common import APP_DIR, app_version

log = logging.getLogger("sonatina")

FILES = ("sonatina.log", "sonatina.log.1", "sonatina.log.2", "sonatina.log.3",
         "dashboard.log", "settings.json")


def desktop_folder() -> str:
    """The real Desktop (it may live in OneDrive), else the profile, else the data folder.
    SONATINA_EXPORT_DIR overrides it (tests)."""
    if os.environ.get("SONATINA_EXPORT_DIR"):
        return os.environ["SONATINA_EXPORT_DIR"]
    if sys.platform == "win32":
        try:
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                            ("d", ctypes.c_ubyte * 8)]
            desktop = GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                           (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
            path = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            if shell32.SHGetKnownFolderPath(ctypes.byref(desktop), 0, None, ctypes.byref(path)) == 0:
                result = path.value
                ctypes.windll.ole32.CoTaskMemFree(path)
                if result and os.path.isdir(result):
                    return result
        except Exception as error:
            log.debug("desktop folder: %r", error)
    home = os.path.join(os.path.expanduser("~"), "Desktop")
    return home if os.path.isdir(home) else APP_DIR


def system_info(extra: dict | None = None) -> str:
    lines = [
        f"Sonatina {app_version()}",
        f"time: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"system: {platform.platform()}",
        f"python: {sys.version.split()[0]} ({'installed' if getattr(sys, 'frozen', False) else 'source'})",
    ]
    try:
        import sonatina_output
        if sys.platform == "win32":
            endpoints, default = sonatina_output.WindowsBackend().list()
            lines.append("playback devices:")
            for e in endpoints:
                lines.append(f"  {'*' if e.id == default else ' '} {e.name}")
    except Exception as error:
        lines.append(f"playback devices: {error!r}")
    try:
        import sounddevice as sd
        lines.append("PortAudio devices:")
        lines.extend("  " + line for line in str(sd.query_devices()).splitlines())
    except Exception as error:
        lines.append(f"PortAudio devices: {error!r}")
    for key, value in (extra or {}).items():
        lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


def export_diagnostics(status: dict, folder: str | None = None, data_dir: str = APP_DIR) -> str:
    """Writes the zip and returns its path."""
    folder = folder or desktop_folder()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, time.strftime("Sonatina-diagnostica-%Y%m%d-%H%M%S.zip"))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            source = os.path.join(data_dir, name)
            if os.path.isfile(source):
                try:
                    z.write(source, name)
                except OSError as error:     # the log is being written: copy what we can
                    with open(source, "rb") as f:
                        z.writestr(name, f.read())
                    log.debug("zip %s: %r", name, error)
        z.writestr("status.json", json.dumps(status, indent=2, ensure_ascii=False, default=str))
        z.writestr("info.txt", system_info())
    log.info("diagnostics exported to %s", path)
    return path


def reveal(path: str) -> None:
    """Opens Explorer with the file selected."""
    if sys.platform == "win32":
        import subprocess
        subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(path)], close_fds=True)
