<#
.SYNOPSIS
    Packages the Tkinter configuration panel into a standalone SonatinaConfig.exe (G2).
#>
[CmdletBinding()]
param(
    [string]$OutDir = '',
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$scriptsDir = $PSScriptRoot
$pkgRoot = Split-Path -Parent $scriptsDir
$entry = Join-Path $scriptsDir 'gui_entry.py'
if (-not $OutDir) {
    $OutDir = Join-Path $pkgRoot 'dist'
}

if ($CheckOnly) {
    Push-Location $pkgRoot
    try {
        & python $entry --hive HKCU --root 'Software\Sonatina-GuiBuildCheck' --selftest
        exit $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}

$pyinstaller = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $pyinstaller) {
    Write-Host 'pyinstaller is not installed. Install with: python -m pip install pyinstaller'
    exit 2
}

Push-Location $pkgRoot
try {
    & $pyinstaller.Source `
        --noconfirm `
        --onefile `
        --windowed `
        --name SonatinaConfig `
        --distpath $OutDir `
        $entry
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
