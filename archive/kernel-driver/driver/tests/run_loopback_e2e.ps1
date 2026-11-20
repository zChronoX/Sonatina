<#
.SYNOPSIS
    Runs the T2 C-driver <-> Python-sink end-to-end UDP loopback test.
#>
[CmdletBinding()]
param(
    [int]$Seconds = 3,
    [int]$SignalPort = 5005,
    [int]$MediaPort = 5004,
    [ValidateSet('sbc', 'ldac')][string]$Codec = 'sbc',
    [switch]$SimulateCongestion
)

$ErrorActionPreference = 'Stop'
$testsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$driverDir = Split-Path -Parent $testsDir
$pkgRoot = Split-Path -Parent $driverDir
$srcDir = Join-Path $driverDir 'src'
$sbcIncDir = Join-Path $driverDir 'third_party\sbc\include'
$sbcSrcDir = Join-Path $driverDir 'third_party\sbc\src'
$ldacIncDir = Join-Path $driverDir 'third_party\ldac\inc'
$ldacSrcDir = Join-Path $driverDir 'third_party\ldac\src'
$outDir = Join-Path $testsDir 'build'
if (-not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir | Out-Null
}

$exe = Join-Path $outDir 'loopback_e2e.exe'
$sources = @(
    (Join-Path $testsDir 'loopback_e2e.c'),
    (Join-Path $srcDir 'abr.c'),
    (Join-Path $srcDir 'avdtp.c'),
    (Join-Path $srcDir 'avdtp_session.c'),
    (Join-Path $srcDir 'rtp.c'),
    (Join-Path $srcDir 'codec_sbc.c'),
    (Join-Path $srcDir 'codec_ldac.c'),
    (Join-Path $srcDir 'media_stream.c'),
    (Join-Path $srcDir 'trace_ring.c'),
    (Join-Path $sbcSrcDir 'bits.c'),
    (Join-Path $sbcSrcDir 'sbc.c')
)
$ldacSources = @(
    (Join-Path $ldacSrcDir 'ldaclib.c'),
    (Join-Path $ldacSrcDir 'ldacBT.c')
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
    Write-Host 'compiling loopback_e2e.exe with cl.exe...'
    & $cl.Source /nologo /c /W3 /wd4100 /wd4127 /wd4189 /wd4244 /wd4245 /wd4267 /wd4456 /wd4457 /wd4459 /wd4701 /wd4702 /wd4706 /D_32BIT_FIXED_POINT /I"$ldacIncDir" /I"$ldacSrcDir" /Fo:"$outDir\" @ldacSources
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $ldacObjs = @((Join-Path $outDir 'ldaclib.obj'), (Join-Path $outDir 'ldacBT.obj'))
    & $cl.Source /nologo /W4 /WX /DSONATINA_HOST /DSONATINA_WITH_LIBSBC /DSONATINA_WITH_LIBLDAC /D_32BIT_FIXED_POINT /I"$srcDir" /I"$sbcIncDir" /I"$ldacIncDir" /Fe:$exe /Fo:"$outDir\" @sources @ldacObjs ws2_32.lib
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
elseif ($clang) {
    Write-Host 'compiling loopback_e2e.exe with clang...'
    $clangTarget = & $clang.Source -dumpmachine 2>$null
    $clangArgs = @('-std=c11', '-Wall', '-Wextra', '-Werror', '-DSONATINA_HOST', '-DSONATINA_WITH_LIBSBC', '-DSONATINA_WITH_LIBLDAC', '-D_32BIT_FIXED_POINT', "-I$srcDir", "-I$sbcIncDir", "-I$ldacIncDir", "-I$ldacSrcDir", '-o', $exe) + $sources + $ldacSources + @('-lws2_32')
    if ($clangTarget -notmatch 'msvc') {
        $clangArgs += '-lm'
    }
    & $clang.Source @clangArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
elseif ($gcc) {
    Write-Host 'compiling loopback_e2e.exe with gcc...'
    & $gcc.Source -std=c11 -Wall -Wextra -Werror -DSONATINA_HOST -DSONATINA_WITH_LIBSBC -DSONATINA_WITH_LIBLDAC -D_32BIT_FIXED_POINT "-I$srcDir" "-I$sbcIncDir" "-I$ldacIncDir" "-I$ldacSrcDir" -o $exe @sources @ldacSources -lws2_32 -lm
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
else {
    Write-Host 'No C compiler found.'
    exit 2
}

$python = (Get-Command python -ErrorAction Stop).Source
$stamp = Get-Date -Format 'yyyyMMddHHmmss'
$sinkOut = Join-Path $env:TEMP ("sonatina-t2-sink-$stamp.log")
$sinkErr = Join-Path $env:TEMP ("sonatina-t2-sink-$stamp.err")
$wavOut = Join-Path $env:TEMP ("sonatina-t2-$stamp.wav")

Push-Location $pkgRoot
$sink = $null
try {
    $sinkArgs = @(
        '-m', 'sonatina', 'sink',
        '--caps', 'pcm,sbc,ldac',
        '--wait', '15',
        '--duration', "$($Seconds + 4)",
        '--idle', '1.0',
        '--out', $wavOut,
        '--signal-port', "$SignalPort",
        '--media-port', "$MediaPort"
    )
    $sink = Start-Process -FilePath $python -ArgumentList $sinkArgs `
        -WorkingDirectory $pkgRoot -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $sinkOut -RedirectStandardError $sinkErr
    $ready = $false
    for ($w = 0; $w -lt 100; $w++) {
        if ($sink.HasExited) { break }
        if ((Test-Path $sinkOut) -and ((Get-Content $sinkOut -Raw) -match 'listening on')) {
            $ready = $true
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if ($sink.HasExited -or -not $ready) {
        Write-Host "sink did not become ready (exited=$($sink.HasExited) code=$($sink.ExitCode))"
        if (Test-Path $sinkOut) { Get-Content $sinkOut }
        if (Test-Path $sinkErr) { Get-Content $sinkErr }
        exit 1
    }

    $cArgs = @('--signal-port', "$SignalPort", '--media-port', "$MediaPort", '--seconds', "$Seconds", '--codec', $Codec)
    if ($SimulateCongestion) {
        $cArgs += '--simulate-congestion'
    }
    & $exe @cArgs
    $cExit = $LASTEXITCODE

    if (-not $sink.WaitForExit(20000)) {
        $sink.Kill()
        Write-Host 'sink did not finish in time'
        exit 1
    }
    $sink.Refresh()
    $sinkExit = if ($null -ne $sink.ExitCode) { $sink.ExitCode } else { 0 }

    $sinkText = ''
    if (Test-Path $sinkOut) {
        $sinkText = Get-Content $sinkOut -Raw
        Write-Host $sinkText.TrimEnd()
    }
    if ((Test-Path $sinkErr) -and (Get-Item $sinkErr).Length -gt 0) {
        Get-Content $sinkErr
    }
    if ($cExit -ne 0 -or $sinkExit -ne 0 -or ($sinkText -notmatch 'packets=[1-9]\d*\s+lost=0\s+errors=0')) {
        Write-Host ("T2 failed: c_exit={0} sink_exit={1}" -f $cExit, $sinkExit)
        exit 1
    }
    exit 0
}
finally {
    if ($null -ne $sink -and -not $sink.HasExited) {
        $sink.Kill()
    }
    if (Test-Path $wavOut) { Remove-Item $wavOut -Force -ErrorAction SilentlyContinue }
    Pop-Location
}
