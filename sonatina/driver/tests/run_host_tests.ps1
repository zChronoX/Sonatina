<#
.SYNOPSIS
    Compiles and runs the C host test harness against Python reference vectors.

.DESCRIPTION
    Searches for cl.exe, clang.exe, or gcc.exe, compiles host_test.c together
    with avdtp.c, rtp.c, and codec_sbc.c (-DSONATINA_HOST), and verifies
    byte-for-byte equality with sonatina/ via generate_vectors.py.
#>
[CmdletBinding()]
param(
    [string]$OutDir = ''
)

$ErrorActionPreference = 'Stop'
$testsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$srcDir = Join-Path (Split-Path -Parent $testsDir) 'src'
if (-not $OutDir) {
    $OutDir = Join-Path $testsDir 'build'
}
if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir | Out-Null
}

$exe = Join-Path $OutDir 'host_test.exe'
$sources = @(
    (Join-Path $testsDir 'host_test.c'),
    (Join-Path $srcDir 'avdtp.c'),
    (Join-Path $srcDir 'avdtp_session.c'),
    (Join-Path $srcDir 'rtp.c'),
    (Join-Path $srcDir 'codec_sbc.c')
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
    Write-Host 'compiling host_test with cl.exe (-DSONATINA_HOST)...'
    & $cl.Source /nologo /W4 /WX /DSONATINA_HOST /Fe:$exe /Fo:"$OutDir\" @sources
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
elseif ($clang) {
    Write-Host 'compiling host_test with clang (-DSONATINA_HOST)...'
    $clangTarget = & $clang.Source -dumpmachine 2>$null
    $clangArgs = @('-std=c11', '-Wall', '-Wextra', '-Werror', '-DSONATINA_HOST', '-o', $exe) + $sources
    if ($clangTarget -notmatch 'msvc') {
        $clangArgs += '-lm'
    }
    & $clang.Source @clangArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
elseif ($gcc) {
    Write-Host 'compiling host_test with gcc (-DSONATINA_HOST)...'
    & $gcc.Source -std=c11 -Wall -Wextra -Werror -DSONATINA_HOST -o $exe @sources -lm
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
else {
    Write-Host 'No C compiler found (cl, clang, or gcc). Host test harness NOT compiled.'
    Write-Host 'Install Visual Studio 2022 Build Tools, LLVM/clang, or MinGW-w64 gcc, then re-run:'
    Write-Host ('  powershell -ExecutionPolicy Bypass -File "{0}"' -f $MyInvocation.MyCommand.Path)
    exit 2
}

$python = (Get-Command python -ErrorAction Stop).Source
$genScript = Join-Path $testsDir 'generate_vectors.py'
& $python $genScript --verify $exe
exit $LASTEXITCODE
