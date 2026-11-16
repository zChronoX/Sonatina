<#
.SYNOPSIS
    Builds SonatinaSvc.exe (native Win32 service) with cl, clang, or gcc.
#>
[CmdletBinding()]
param(
    [string]$OutDir = ''
)

$ErrorActionPreference = 'Stop'
$svcDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Split-Path -Parent $svcDir
$drvIncDir = Join-Path $rootDir 'driver\include'
$drvSrcDir = Join-Path $rootDir 'driver\src'

if (-not $OutDir) {
    $OutDir = Join-Path $svcDir 'build'
}
if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir | Out-Null
}

$exe = Join-Path $OutDir 'SonatinaSvc.exe'
$sources = @(
    (Join-Path $svcDir 'service_main.c'),
    (Join-Path $svcDir 'apply_policy.c'),
    (Join-Path $svcDir 'reg_store.c'),
    (Join-Path $svcDir 'device_ctrl.c'),
    (Join-Path $drvSrcDir 'profile_validate.c')
)

$cl = Get-Command cl -ErrorAction SilentlyContinue
$clang = Get-Command clang -ErrorAction SilentlyContinue
$gcc = Get-Command gcc -ErrorAction SilentlyContinue

if (-not $cl -and -not $clang -and -not $gcc) {
    $wingetRoots = @(
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages')
    )
    foreach ($wr in $wingetRoots) {
        if (Test-Path $wr) {
            $foundGcc = Get-ChildItem -Path $wr -Filter 'gcc.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($foundGcc) {
                $env:PATH = ($foundGcc.DirectoryName + ';' + $env:PATH)
                $gcc = Get-Command gcc -ErrorAction SilentlyContinue
                break
            }
        }
    }
}

if ($cl) {
    Write-Host 'compiling SonatinaSvc.exe with cl.exe...'
    & $cl.Source /nologo /W4 /WX /D_CRT_SECURE_NO_WARNINGS /I"$svcDir" /I"$drvIncDir" /I"$drvSrcDir" /Fe:$exe /Fo:"$OutDir\" @sources advapi32.lib setupapi.lib cfgmgr32.lib shell32.lib
    exit $LASTEXITCODE
}
elseif ($clang) {
    Write-Host 'compiling SonatinaSvc.exe with clang...'
    $clangArgs = @('-std=c11', '-Wall', '-Wextra', '-Werror', '-D_CRT_SECURE_NO_WARNINGS', "-I$svcDir", "-I$drvIncDir", "-I$drvSrcDir", '-o', $exe) + $sources + @('-ladvapi32', '-lsetupapi', '-lcfgmgr32', '-lshell32')
    & $clang.Source @clangArgs
    exit $LASTEXITCODE
}
elseif ($gcc) {
    Write-Host 'compiling SonatinaSvc.exe with gcc...'
    & $gcc.Source -std=c11 -Wall -Wextra -Werror "-I$svcDir" "-I$drvIncDir" "-I$drvSrcDir" -o $exe @sources -ladvapi32 -lsetupapi -lcfgmgr32 -lshell32
    exit $LASTEXITCODE
}
else {
    Write-Host 'No C compiler found (cl, clang, or gcc).'
    exit 2
}
