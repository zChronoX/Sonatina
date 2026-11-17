<#
.SYNOPSIS
    Prepares and checks the test VM before installing SonatinaAudio (run as Administrator).

.DESCRIPTION
    - shows the Bluetooth AudioSink (A2DP) devices and the driver they use
    - checks Secure Boot and test signing
    - enables kernel DbgPrint output for DebugView (Debug Print Filter)
    Safe to run more than once.
#>
$ErrorActionPreference = 'Continue'

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Warning 'Esegui PowerShell come amministratore.'
    return
}

Write-Host '== Cuffie (Bluetooth AudioSink / A2DP)' -ForegroundColor Cyan
$devs = Get-PnpDevice | Where-Object { $_.HardwareID -like '*0000110b*' }
if (-not $devs) {
    Write-Warning 'Nessun dispositivo A2DP trovato: abbina e connetti le cuffie.'
}
foreach ($d in $devs) {
    $inf = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName DEVPKEY_Device_DriverInfPath -ErrorAction SilentlyContinue).Data
    $prov = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName DEVPKEY_Device_DriverProvider -ErrorAction SilentlyContinue).Data
    Write-Host ("   {0}  [{1}]" -f $d.FriendlyName, $d.Status)
    Write-Host ("   InstanceId: {0}" -f $d.InstanceId)
    Write-Host ("   Driver: {0} ({1})" -f $inf, $prov)
}

Write-Host '== Avvio protetto e firma di test' -ForegroundColor Cyan
try {
    $sb = Confirm-SecureBootUEFI
    Write-Host ("   Secure Boot attivo: {0}" -f $sb)
    if ($sb) { Write-Warning 'Disattiva Secure Boot nelle impostazioni della VM (Options > Advanced).' }
} catch {
    Write-Host '   Secure Boot: non disponibile/disattivato'
}
$bcd = (& bcdedit /enum '{current}' 2>&1) -join "`n"
$ts = $bcd -match 'testsigning\s+(Yes|S)'
Write-Host ("   Test signing: {0}" -f $(if ($ts) { 'ATTIVO' } else { 'spento' }))
if (-not $ts) {
    & bcdedit /set testsigning on | Out-Null
    Write-Warning 'Test signing attivato ora: serve un riavvio.'
}

Write-Host '== Log del kernel per DebugView' -ForegroundColor Cyan
$key = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Debug Print Filter'
if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
$cur = (Get-ItemProperty -Path $key -Name DEFAULT -ErrorAction SilentlyContinue).DEFAULT
if ($cur -ne 0xF) {
    New-ItemProperty -Path $key -Name DEFAULT -PropertyType DWord -Value 0xF -Force | Out-Null
    Write-Warning 'Debug Print Filter impostato: serve un riavvio.'
} else {
    Write-Host '   Debug Print Filter: OK'
}

Write-Host ''
Write-Host 'Fatto. Se sopra c''e'' scritto "serve un riavvio", riavvia la VM e rilancia lo script.' -ForegroundColor Green
