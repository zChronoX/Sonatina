<#
.SYNOPSIS
    Runs the Sonatina loopback demo: headset simulator plus Bluetooth audio source.

.DESCRIPTION
    Starts the sink in the background, runs the source for the requested number
    of seconds, waits for the sink to finish and checks that the WAV file was
    written. Exits 0 only when both processes succeed and the file is non-empty.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run-demo.ps1 -Seconds 6
#>
[CmdletBinding()]
param(
    [int]$Seconds = 6,
    [ValidateSet('pcm', 'sbc', 'aac', 'aptx')]
    [string]$Codec = 'sbc',
    [string]$Device = '001122AABBCC',
    [int]$MediaPort = 5004,
    [int]$SignalPort = 5005,
    [string]$Out = 'out.wav'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format 'yyyyMMddHHmmss'
$sinkOut = Join-Path $env:TEMP ("sonatina-sink-$stamp.log")
$sinkErr = Join-Path $env:TEMP ("sonatina-sink-$stamp.err")
$wavPath = if ([System.IO.Path]::IsPathRooted($Out)) { $Out } else { Join-Path $root $Out }

Push-Location $root
$sink = $null
try {
    $python = (Get-Command python -ErrorAction Stop).Source

    $sinkArgs = @(
        '-m', 'sonatina', 'sink',
        '--caps', 'pcm,sbc',
        '--wait', '20',
        '--duration', "$($Seconds + 4)",
        '--out', $wavPath,
        '--media-port', "$MediaPort",
        '--signal-port', "$SignalPort"
    )
    Write-Host "starting sink (caps=pcm,sbc, ports=$SignalPort/$MediaPort)"
    $sink = Start-Process -FilePath $python -ArgumentList $sinkArgs `
        -WorkingDirectory $root -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $sinkOut -RedirectStandardError $sinkErr

    Start-Sleep -Milliseconds 900
    if ($sink.HasExited) {
        Write-Host "sink exited early (code $($sink.ExitCode))"
        if (Test-Path $sinkErr) { Get-Content $sinkErr }
        exit 1
    }

    Write-Host "running source (codec=$Codec, device=$Device, ${Seconds}s)"
    & $python -m sonatina source --device $Device --codec $Codec `
        --duration $Seconds --media-port $MediaPort --signal-port $SignalPort
    $sourceExit = $LASTEXITCODE

    if (-not $sink.WaitForExit(30000)) {
        Write-Host 'sink did not stop in time'
        $sink.Kill()
        exit 1
    }
    $sink.Refresh()
    $sinkExit = $sink.ExitCode
    if ($null -eq $sinkExit) { $sinkExit = 0 }

    if (Test-Path $sinkOut) {
        Write-Host '--- sink log ---'
        Get-Content $sinkOut
    }
    if ((Test-Path $sinkErr) -and (Get-Item $sinkErr).Length -gt 0) {
        Write-Host '--- sink stderr ---'
        Get-Content $sinkErr
    }

    $wavOk = (Test-Path $wavPath) -and ((Get-Item $wavPath).Length -gt 44)
    Write-Host ("source_exit={0} sink_exit={1} wav_ok={2} wav={3}" -f `
        $sourceExit, $sinkExit, $wavOk, $wavPath)

    if ($sourceExit -ne 0 -or $sinkExit -ne 0 -or -not $wavOk) {
        exit 1
    }
    exit 0
}
finally {
    if ($null -ne $sink -and -not $sink.HasExited) {
        $sink.Kill()
    }
    Pop-Location
}
