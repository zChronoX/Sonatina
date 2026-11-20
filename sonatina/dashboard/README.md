# Sonatina dashboard

WinUI 3 window (C#, .NET 8, Windows App SDK, unpackaged and self-contained) over the tray
process. It talks to `sonatina_tray.py` through `sonatina_ipc.py`: TCP on 127.0.0.1, port and
token in `%LOCALAPPDATA%\Sonatina\ipc.json`, newline-delimited JSON (see the docstring there).

- Opens from the tray ("Apri Sonatina", or a left click on the icon), or by launching
  Sonatina again while it runs. One window only: a second launch brings it to the front.
- Closing the window does not stop Sonatina: the tray keeps playing.
- Italian when Windows is in Italian, English otherwise; light/dark theme from Windows.

Build (needs the .NET 8 SDK: `winget install Microsoft.DotNet.SDK.8`):

    powershell -ExecutionPolicy Bypass -File sonatina\dashboard\build.ps1 -Run

The installer ships it in `{app}\dashboard\` (CI job `dashboard`).
