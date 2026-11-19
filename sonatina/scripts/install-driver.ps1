<#
.SYNOPSIS
    Installs SonatinaAudio.sys and SonatinaSvc.exe with automatic rollback on failure.

.DESCRIPTION
    Validates package artefacts, optionally checks test signing, stages binaries
    into Program Files, publishes SonatinaAudio.inf via pnputil, creates and
    starts SonatinaSvc, and rolls back cleanly if any step fails.
    Use -DryRun to verify staging and commands without modifying system state.

    -Force        binds SonatinaAudio.inf to every present Bluetooth AudioSink devnode
                  (BTHENUM\{0000110b-...}) even though the inbox Windows driver ranks
                  higher (same as `devcon update`). Needed for test-signed builds.
    -SkipService  installs only the kernel driver (enough for the first tests).
#>
[CmdletBinding()]
param(
    [string]$DriverDir = '',
    [string]$ServiceExe = '',
    [string]$InstallDir = '',
    [switch]$AllowUnsigned,
    [switch]$Force,
    [switch]$SkipService,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repoPkg = Split-Path -Parent $PSScriptRoot

if (-not $DriverDir) {
    $candidates = @(
        (Join-Path $repoPkg 'driver\build\x64\Release\SonatinaAudio'),
        (Join-Path $repoPkg 'driver\build\x64\Debug\SonatinaAudio'),
        (Join-Path $repoPkg 'driver\inf')
    )
    foreach ($c in $candidates) {
        if (Test-Path (Join-Path $c 'SonatinaAudio.inf')) {
            $DriverDir = $c
            break
        }
    }
}
if (-not $ServiceExe) {
    $ServiceExe = Join-Path $repoPkg 'service\build\SonatinaSvc.exe'
}
if (-not $InstallDir) {
    $InstallDir = Join-Path $env:ProgramFiles 'Sonatina'
}

$infPath = Join-Path $DriverDir 'SonatinaAudio.inf'
$sysPath = Join-Path $DriverDir 'SonatinaAudio.sys'
if (-not (Test-Path $infPath)) {
    throw "SonatinaAudio.inf not found in $DriverDir"
}
if (-not $DryRun -and -not (Test-Path $sysPath)) {
    throw "SonatinaAudio.sys not found in $DriverDir (build WDK package first)"
}
if (-not $SkipService -and -not (Test-Path $ServiceExe)) {
    throw "SonatinaSvc.exe not found at $ServiceExe (run service/build_service.ps1, or pass -SkipService)"
}
$audioSinkHwid = 'BTHENUM\{0000110b-0000-1000-8000-00805f9b34fb}'

if ($DryRun) {
    Write-Host ("[dry-run] DriverDir   = {0}" -f $DriverDir)
    Write-Host ("[dry-run] ServiceExe  = {0}" -f $ServiceExe)
    Write-Host ("[dry-run] InstallDir  = {0}" -f $InstallDir)
    Write-Host ("[dry-run] Would run: pnputil /add-driver `"{0}`" /install" -f $infPath)
    if ($Force) {
        Write-Host ("[dry-run] Would force-bind {0} with UpdateDriverForPlugAndPlayDevices" -f $audioSinkHwid)
    }
    if (-not $SkipService) {
        Write-Host ("[dry-run] Would create service SonatinaSvc -> `"{0}`"" -f (Join-Path $InstallDir 'SonatinaSvc.exe'))
    }
    exit 0
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'install-driver.ps1 requires Administrator privileges (or pass -DryRun).'
}

if (-not $AllowUnsigned) {
    $bcd = (& bcdedit /enum '{current}' 2>&1) -join "`n"
    $catPath = Join-Path $DriverDir 'SonatinaAudio.cat'
    $testSigningOn = ($bcd -match 'testsigning\s+Yes')
    if (-not $testSigningOn -and -not (Test-Path $catPath)) {
        throw 'Test signing is OFF and no signed catalog (.cat) was found. Use -AllowUnsigned only if using an attestation-signed package.'
    }
}

$publishedOem = $null
$createdService = $false
$stagedDir = $false

try {
    if (-not (Test-Path $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
        $stagedDir = $true
    }
    $targetSvc = Join-Path $InstallDir 'SonatinaSvc.exe'
    if (-not $SkipService) {
        Copy-Item -Path $ServiceExe -Destination $targetSvc -Force
    }

    $pnpOut = (& pnputil /add-driver $infPath /install 2>&1)
    # 0 = ok, 3010 = reboot required, 259 (ERROR_NO_MORE_ITEMS) = package already in the
    # driver store / no device needed updating: not a failure (-Force binds explicitly below).
    if ($LASTEXITCODE -notin 0, 259, 3010) {
        throw ("pnputil /add-driver failed ({0}): {1}" -f $LASTEXITCODE, ($pnpOut -join "`n"))
    }
    $joined = $pnpOut -join "`n"
    if ($joined -match '(oem\d+\.inf)') {
        $publishedOem = $Matches[1]
    }
    $pubLabel = if ($publishedOem) { $publishedOem } else { 'published' }
    Write-Host ("Driver installed ({0})" -f $pubLabel)

    if ($Force) {
        if (-not ('Sonatina.NewDev' -as [type])) {
            Add-Type -Namespace Sonatina -Name NewDev -MemberDefinition @'
[DllImport("newdev.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern bool UpdateDriverForPlugAndPlayDevices(IntPtr hwndParent, string hardwareId,
    string fullInfPath, uint installFlags, out bool rebootRequired);
'@
        }
        $fullInf = (Resolve-Path $infPath).Path
        $reboot = $false
        # INSTALLFLAG_FORCE = 0x1: install even if the inbox driver is a better match.
        $ok = [Sonatina.NewDev]::UpdateDriverForPlugAndPlayDevices([IntPtr]::Zero, $audioSinkHwid, $fullInf, 1, [ref]$reboot)
        if (-not $ok) {
            $err = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            if (($err -band 0xFFFFFFFF) -eq 0xE000020B) {
                throw 'No Bluetooth AudioSink device is present: pair and connect the headset first.'
            }
            throw ("UpdateDriverForPlugAndPlayDevices failed (Win32 error 0x{0:X8})" -f ($err -band 0xFFFFFFFF))
        }
        $suffix = if ($reboot) { ' (reboot required)' } else { '' }
        Write-Host ("SonatinaAudio bound to {0}{1}" -f $audioSinkHwid, $suffix)
    }

    if ($SkipService) {
        Write-Host 'SonatinaSvc not installed (-SkipService).'
        exit 0
    }

    & sc.exe query SonatinaSvc | Out-Null
    if ($LASTEXITCODE -ne 0) {
        & sc.exe create SonatinaSvc "binPath= `"$targetSvc`"" start= auto obj= LocalSystem "DisplayName= Sonatina Bluetooth Audio Service" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'sc.exe create SonatinaSvc failed' }
        $createdService = $true
    } else {
        & sc.exe stop SonatinaSvc | Out-Null
        Start-Sleep -Milliseconds 500
        & sc.exe config SonatinaSvc "binPath= `"$targetSvc`"" start= auto | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'sc.exe config SonatinaSvc failed' }
    }

    & sc.exe start SonatinaSvc | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'sc.exe start SonatinaSvc failed' }
    Write-Host 'SonatinaSvc started successfully.'
    exit 0
}
catch {
    Write-Warning ("Install failed: {0}. Rolling back..." -f $_)
    if ($createdService) {
        & sc.exe stop SonatinaSvc | Out-Null
        & sc.exe delete SonatinaSvc | Out-Null
    }
    if ($publishedOem) {
        & pnputil /delete-driver $publishedOem /uninstall /force | Out-Null
    }
    if ($stagedDir -and (Test-Path $InstallDir)) {
        Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    throw
}
