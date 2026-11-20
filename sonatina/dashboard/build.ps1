# Builds the dashboard into sonatina\dashboard\publish (where the tray looks for it in a
# checkout) and writes the full output to local\dashboard-build.log.
#   cd C:\Users\Giovanni\Desktop\Sonatina\Sonatina
#   powershell -ExecutionPolicy Bypass -File sonatina\dashboard\build.ps1 [-Run]
param([switch]$Run)

$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Resolve-Path (Join-Path $here '..\..')
$log = Join-Path $repo 'local\dashboard-build.log'
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    Write-Host "dotnet non trovato: winget install Microsoft.DotNet.SDK.8"
    exit 1
}
Set-Content -Path $log -Value "=== $(Get-Date -Format s)" -Encoding UTF8
dotnet --list-sdks 2>&1 | ForEach-Object { Add-Content -Path $log -Value "$_" -Encoding UTF8 }

Get-Process SonatinaDashboard -ErrorAction SilentlyContinue | Stop-Process -Force
dotnet publish (Join-Path $here 'SonatinaDashboard.csproj') -c Release -r win-x64 --self-contained true `
    -p:Platform=x64 -o (Join-Path $here 'publish') -nologo -clp:NoSummary 2>&1 |
    ForEach-Object { $_; Add-Content -Path $log -Value "$_" -Encoding UTF8 }
$code = $LASTEXITCODE
Add-Content -Path $log -Value "=== exit $code" -Encoding UTF8

if ($code -eq 0) {
    Write-Host "OK: $(Join-Path $here 'publish\SonatinaDashboard.exe')" -ForegroundColor Green
    if ($Run) { Start-Process (Join-Path $here 'publish\SonatinaDashboard.exe') }
} else {
    Write-Host "Build fallita: log in $log" -ForegroundColor Red
}
exit $code
