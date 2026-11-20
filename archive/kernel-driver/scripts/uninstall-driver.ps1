<#
.SYNOPSIS
    Uninstalls SonatinaSvc and SonatinaAudio.sys, restoring inbox Bluetooth audio.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = '',
    [switch]$RemoveFiles,
    [switch]$PurgeRegistry,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
if (-not $InstallDir) {
    $InstallDir = Join-Path $env:ProgramFiles 'Sonatina'
}

if ($DryRun) {
    Write-Host '[dry-run] Would stop and delete service SonatinaSvc'
    Write-Host '[dry-run] Would enumerate pnputil /enum-drivers for SonatinaAudio.inf and delete matching OEM INF(s)'
    Write-Host '[dry-run] Would run pnputil /scan-devices to restore inbox BthA2dp'
    if ($RemoveFiles) { Write-Host ("[dry-run] Would remove {0}" -f $InstallDir) }
    if ($PurgeRegistry) { Write-Host '[dry-run] Would delete HKLM\SYSTEM\CurrentControlSet\Services\SonatinaAudio\Parameters' }
    exit 0
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'uninstall-driver.ps1 requires Administrator privileges (or pass -DryRun).'
}

& sc.exe query SonatinaSvc | Out-Null
if ($LASTEXITCODE -eq 0) {
    & sc.exe stop SonatinaSvc | Out-Null
    Start-Sleep -Milliseconds 500
    & sc.exe delete SonatinaSvc | Out-Null
}

$enumOut = (& pnputil /enum-drivers 2>&1) -join "`n"
$blocks = $enumOut -split '(\r?\n){2,}'
foreach ($b in $blocks) {
    if ($b -match 'SonatinaAudio\.inf' -and $b -match '(oem\d+\.inf)') {
        $oem = $Matches[1]
        Write-Host ("Removing driver package {0}..." -f $oem)
        & pnputil /delete-driver $oem /uninstall /force | Out-Null
    }
}

& pnputil /scan-devices | Out-Null

if ($RemoveFiles -and (Test-Path $InstallDir)) {
    Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
}
if ($PurgeRegistry) {
    reg delete 'HKLM\SYSTEM\CurrentControlSet\Services\SonatinaAudio\Parameters' /f 2>$null | Out-Null
}

Write-Host 'Sonatina driver and service uninstalled.'
exit 0
