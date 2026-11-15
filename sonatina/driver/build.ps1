<#
.SYNOPSIS
    Builds SonatinaAudio.sys and validates the driver package on a machine with VS + WDK.

.DESCRIPTION
    Checks for MSBuild (via PATH or vswhere.exe) and Windows SDK/WDK 10, builds
    SonatinaAudio.vcxproj, and runs InfVerif /w and inf2cat when available.
    Does not install any tools or certificates.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build.ps1 -Configuration Debug -Platform x64
#>
[CmdletBinding()]
param(
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Debug',
    [ValidateSet('x64', 'ARM64')]
    [string]$Platform = 'x64',
    [switch]$SkipInfCheck
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-MsBuild {
    $cmd = Get-Command msbuild -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path $vswhere) {
        $found = & $vswhere -latest -requires Microsoft.Component.MSBuild -find 'MSBuild\**\Bin\MSBuild.exe' | Select-Object -First 1
        if ($found -and (Test-Path $found)) { return $found }
    }
    return $null
}

function Find-KitTool([string]$ToolName, [string]$KitsRoot) {
    $cmd = Get-Command $ToolName -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $binRoot = Join-Path $KitsRoot 'bin'
    if (-not (Test-Path $binRoot)) { return $null }

    $matches = @(Get-ChildItem -Path $binRoot -Filter $ToolName -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\' -or $_.FullName -match '\\x86\\' } |
        Sort-Object FullName -Descending)
    if ($matches.Count -gt 0) { return $matches[0].FullName }
    return $null
}

$msbuildPath = Find-MsBuild
if (-not $msbuildPath) {
    Write-Host 'msbuild not found. Install Visual Studio 2022 Build Tools with the C++ workload.'
    exit 2
}

$kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10'
if (-not (Test-Path (Join-Path $kits 'Include'))) {
    Write-Host 'Windows SDK/WDK not found under C:\Program Files (x86)\Windows Kits\10.'
    Write-Host 'Install "Windows Driver Kit" from the Visual Studio / WDK installer.'
    exit 2
}

$project = Join-Path $root 'SonatinaAudio.vcxproj'
if (-not (Test-Path $project)) {
    Write-Host 'SonatinaAudio.vcxproj is not present.'
    exit 2
}

Write-Host ("building {0} | {1} | {2}" -f $project, $Configuration, $Platform)
& $msbuildPath $project /m /p:Configuration=$Configuration /p:Platform=$Platform
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if (-not $SkipInfCheck) {
    $infDir = Join-Path $root 'inf'
    $infFile = Join-Path $infDir 'SonatinaAudio.inf'

    $infVerif = Find-KitTool 'InfVerif.exe' $kits
    if ($infVerif) {
        Write-Host ("running InfVerif /w on {0}" -f $infFile)
        & $infVerif /w $infFile
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    else {
        Write-Host 'InfVerif.exe not found in Windows Kits; skipping InfVerif.'
    }

    $inf2cat = Find-KitTool 'inf2cat.exe' $kits
    $pkgDir = Join-Path $root ("build\{0}\{1}\SonatinaAudio" -f $Platform, $Configuration)
    if ($inf2cat -and (Test-Path $pkgDir)) {
        $osArg = if ($Platform -eq 'ARM64') { '10_VB_ARM64' } else { '10_X64' }
        Write-Host ("running inf2cat /driver:{0} /os:{1}" -f $pkgDir, $osArg)
        & $inf2cat "/driver:$pkgDir" "/os:$osArg"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    elseif (-not $inf2cat) {
        Write-Host 'inf2cat.exe not found in Windows Kits; skipping inf2cat.'
    }
}

exit 0
