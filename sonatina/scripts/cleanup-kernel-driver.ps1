# Sonatina - passo 3: rimozione completa del driver kernel e degli strumenti di sviluppo.
#
# Prova (non cambia niente, mostra cosa farebbe):
#   powershell -ExecutionPolicy Bypass -File cleanup-kernel-driver.ps1
# Esecuzione vera (PowerShell come amministratore):
#   powershell -ExecutionPolicy Bypass -File cleanup-kernel-driver.ps1 -Apply
#
# NON tocca: SonatinaHost (user-mode), la chiavetta Realtek su WinUSB e il suo certificato
# libwdi, VB-CABLE, Python, Git, i tuoi programmi e le tue librerie Python.
param(
    [switch]$Apply,
    [switch]$KeepWindowsPairing,   # non rimuovere le CMF dal Bluetooth di Windows
    [switch]$KeepPrograms,         # non disinstallare WinLibs / WiX / VMware
    [string]$HeadsetAddress = '2CBEEE4EED2E'
)
$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$local = Join-Path $repo 'local'
New-Item -ItemType Directory -Force $local | Out-Null
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($Apply -and -not $isAdmin) { throw "Serve PowerShell come amministratore per -Apply." }
if ($Apply) { Start-Transcript -Path (Join-Path $local 'cleanup.log') -Append | Out-Null }

$mode = if ($Apply) { 'ESEGUO' } else { 'PROVA ' }
function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Do-It($text, [scriptblock]$action) {
    Write-Host "  [$mode] $text"
    if ($Apply) { try { & $action } catch { Write-Host "    ! $($_.Exception.Message)" -ForegroundColor Yellow } }
}

# ---------------------------------------------------------------------------------------------
Step "0. Punto di ripristino"
Do-It "Checkpoint-Computer 'Prima della pulizia Sonatina'" {
    Checkpoint-Computer -Description 'Prima della pulizia Sonatina' -RestorePointType MODIFY_SETTINGS
}

# ---------------------------------------------------------------------------------------------
Step "1. Pacchetti del driver kernel nello store di Windows"
$packages = @(Get-WindowsDriver -Online -All -ErrorAction SilentlyContinue |
    Where-Object { $_.OriginalFileName -like '*sonatinaaudio.inf' })
if ($packages.Count -eq 0) { Write-Host "  nessun pacchetto Sonatina trovato" }
foreach ($p in $packages) {
    $inf = $p.Driver
    Do-It "pnputil /delete-driver $inf /uninstall /force   ($($p.Version))" {
        pnputil /delete-driver $inf /uninstall /force | Out-Host
    }
}

# ---------------------------------------------------------------------------------------------
Step "2. Servizio SonatinaAudio e profili nel registro"
if (Get-Service SonatinaAudio -ErrorAction SilentlyContinue) {
    Do-It "sc.exe delete SonatinaAudio" { sc.exe stop SonatinaAudio | Out-Null; sc.exe delete SonatinaAudio | Out-Host }
} else { Write-Host "  servizio non presente" }
$svcKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\SonatinaAudio'
if (Test-Path $svcKey) { Do-It "rimuovo $svcKey" { Remove-Item $svcKey -Recurse -Force } }
Do-It "pnputil /scan-devices (le cuffie tornano al driver Microsoft)" { pnputil /scan-devices | Out-Host }

# ---------------------------------------------------------------------------------------------
Step "3. Cuffie nel Bluetooth di Windows"
if ($KeepWindowsPairing) {
    Write-Host "  lasciate abbinate (-KeepWindowsPairing)"
} else {
    Do-It "rimuovo l'abbinamento Windows di $HeadsetAddress (restano abbinate alla chiavetta Sonatina)" {
        Add-Type -Namespace Sonatina -Name Bt -MemberDefinition @'
[DllImport("BluetoothApis.dll", SetLastError = true)]
public static extern uint BluetoothRemoveDevice(ref ulong pAddress);
'@
        [ulong]$addr = [Convert]::ToUInt64($HeadsetAddress, 16)
        $rc = [Sonatina.Bt]::BluetoothRemoveDevice([ref]$addr)
        if ($rc -eq 0) { Write-Host "    rimosse" } else { Write-Host "    codice $rc (gia' rimosse o radio spenta: toglile da Impostazioni > Bluetooth)" }
    }
}

# ---------------------------------------------------------------------------------------------
Step "4. Driver Verifier e test signing"
$verifier = (verifier /querysettings) 2>&1 | Out-String
if ($verifier -match '(?i)sonatina') {
    Do-It "verifier /reset (era attivo su SonatinaAudio.sys)" { verifier /reset | Out-Host }
} else { Write-Host "  Driver Verifier non attivo su Sonatina" }
Do-It "bcdedit /set testsigning off" { bcdedit /set testsigning off | Out-Host }
Do-It "bcdedit /deletevalue nointegritychecks (se presente)" { bcdedit /deletevalue nointegritychecks 2>&1 | Out-Null }

# ---------------------------------------------------------------------------------------------
Step "5. Programmi installati solo per il driver kernel"
if ($KeepPrograms) {
    Write-Host "  lasciati (-KeepPrograms)"
} else {
    foreach ($id in @('BrechtSanders.WinLibs.POSIX.UCRT', 'WiXToolset.WiXCLI')) {
        Do-It "winget uninstall --id $id --exact" { winget uninstall --id $id --exact --accept-source-agreements | Out-Host }
    }
    Do-It "winget uninstall 'VMware Workstation' (l'uninstaller puo' chiedere conferma)" {
        winget uninstall --name 'VMware Workstation' --accept-source-agreements | Out-Host
    }
    $vms = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Virtual Machines'
    if (Test-Path $vms) {
        $size = [math]::Round(((Get-ChildItem $vms -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object Length -Sum).Sum / 1GB), 1)
        Write-Host "  macchine virtuali VMware in $vms ($size GB): le elimini tu a mano se non servono"
    }
}

# ---------------------------------------------------------------------------------------------
Step "6. Librerie Python di Sonatina nel Python di sistema (SonatinaHost usa la sua .venv)"
$pyPkgs = 'bumble', 'sounddevice', 'pycaw', 'comtypes', 'libusb1', 'libusb-package', 'pyusb', 'pyee', 'pyserial-asyncio'
Do-It "python -m pip uninstall -y $($pyPkgs -join ' ')" { python -m pip uninstall -y @pyPkgs | Out-Host }

# ---------------------------------------------------------------------------------------------
Step "7. File scaricati"
$dl = Join-Path $env:USERPROFILE 'Downloads'
foreach ($name in @('Sonatina-VM-Debug-x64', 'Sonatina-VM-Debug-x64.zip', 'VBCABLE_Driver_Pack46.zip')) {
    $path = Join-Path $dl $name
    if (Test-Path $path) { Do-It "elimino $path" { Remove-Item $path -Recurse -Force } }
}
$zadig = Join-Path $dl 'zadig-2.9.exe'
if (Test-Path $zadig) {
    $tools = Join-Path $local 'tools'
    Do-It "sposto zadig-2.9.exe in $tools (serve per ridare la chiavetta a Windows)" {
        New-Item -ItemType Directory -Force $tools | Out-Null; Move-Item $zadig $tools -Force
    }
}

# ---------------------------------------------------------------------------------------------
Step "Fatto"
if ($Apply) {
    Write-Host "Ora: 1) riavvia, 2) nel BIOS attiva Secure Boot (chiavi standard/di fabbrica),"
    Write-Host "     3) in Windows: msinfo32 -> 'Stato avvio protetto: Attivato', 4) avvia LoL/Valorant."
    Stop-Transcript | Out-Null
} else {
    Write-Host "Era una prova: niente e' stato modificato. Rilancia con -Apply (come amministratore)."
}
