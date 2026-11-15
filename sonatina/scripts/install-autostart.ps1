<#
.SYNOPSIS
    Installs or removes the Sonatina settings service as a logon task.

.DESCRIPTION
    Registers a per-user scheduled task that runs `python -m sonatina svc` at
    logon. No service account or pywin32 is required. Registering a task with
    the highest run level may require an elevated shell.

.PARAMETER Remove
    Unregisters the task instead of creating it.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'Sonatina Service',
    [string]$PythonPath,
    [string]$Root,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
if (-not $Root) {
    $Root = Split-Path -Parent $PSScriptRoot
}
$Root = (Resolve-Path -LiteralPath $Root).Path
if (-not $PythonPath) {
    $PythonPath = (Get-Command python -ErrorAction Stop).Source
}

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Host "task '$TaskName' is not registered"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "task '$TaskName' removed"
    exit 0
}

$action = New-ScheduledTaskAction -Execute $PythonPath `
    -Argument '-m sonatina svc' -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -RunLevel Highest `
    -Description 'Sonatina settings watcher' -Force | Out-Null

Write-Host "task '$TaskName' registered"
Write-Host ("  python : {0}" -f $PythonPath)
Write-Host ("  root   : {0}" -f $Root)
