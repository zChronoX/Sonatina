# Installs SonatinaHost for the current user: private Python environment (.venv),
# tray app started at logon (shortcut in the Startup folder), started right away.
#   powershell -ExecutionPolicy Bypass -File install-host.ps1             # install / update
#   powershell -ExecutionPolicy Bypass -File install-host.ps1 -Uninstall  # remove autostart
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $here '.venv'
$pythonw = Join-Path $venv 'Scripts\pythonw.exe'
$tray = Join-Path $here 'sonatina_tray.py'
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Sonatina.lnk'

function Stop-Sonatina {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*sonatina_tray.py*' -or $_.CommandLine -like '*sonatina_host.py*' } |
        ForEach-Object { Write-Host "stopping PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force }
}

if ($Uninstall) {
    Stop-Sonatina
    if (Test-Path $lnk) { Remove-Item $lnk; Write-Host "autostart removed" }
    Write-Host "(the .venv folder can be deleted by hand)"
    return
}

Stop-Sonatina
if (-not (Test-Path $pythonw)) {
    Write-Host "creating $venv"
    python -m venv $venv
}
& (Join-Path $venv 'Scripts\python.exe') -m pip install --upgrade pip --quiet
& (Join-Path $venv 'Scripts\python.exe') -m pip install -r (Join-Path $here 'requirements.txt') --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = $pythonw
$s.Arguments = "`"$tray`""
$s.WorkingDirectory = $here
$s.Description = 'Sonatina: A2DP verso le cuffie'
$s.Save()
Write-Host "autostart: $lnk"

Start-Process -FilePath $pythonw -ArgumentList "`"$tray`"" -WorkingDirectory $here
Write-Host "SonatinaHost started (tray icon). Log: $env:LOCALAPPDATA\Sonatina\sonatina.log"
