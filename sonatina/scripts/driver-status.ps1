<#
.SYNOPSIS
    Shows the live state and the recent event trace of SonatinaAudio.sys.

.DESCRIPTION
    Opens every SonatinaAudio control interface and reads IOCTL_SONATINA_GET_ADDRESS
    and IOCTL_SONATINA_GET_STATS (see driver/include/public.h). Needs no Python and
    no admin rights. Use -Watch to refresh every second.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File driver-status.ps1 -Watch
#>
[CmdletBinding()]
param(
    [switch]$Watch
)

$ErrorActionPreference = 'Stop'

if (-not ('Sonatina.Ctl' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace Sonatina {
public static class Ctl {
    static readonly Guid Iface = new Guid("6e1f2c7a-9b4d-4e6a-8c31-2f5b7d9a1e42");
    public const uint IOCTL_GET_STATS   = 0x83316010; // CTL_CODE(0x8331, 0x804, METHOD_BUFFERED, FILE_READ_ACCESS)
    public const uint IOCTL_GET_ADDRESS = 0x83316018; // CTL_CODE(0x8331, 0x806, METHOD_BUFFERED, FILE_READ_ACCESS)

    [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
    static extern int CM_Get_Device_Interface_List_SizeW(out uint len, ref Guid g, string dev, uint flags);
    [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
    static extern int CM_Get_Device_Interface_ListW(ref Guid g, string dev, char[] buf, uint len, uint flags);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr sa, uint disp, uint flags, IntPtr tmpl);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool DeviceIoControl(SafeFileHandle h, uint code, IntPtr inBuf, uint inLen,
                                       byte[] outBuf, uint outLen, out uint returned, IntPtr ov);

    public static string[] Interfaces() {
        Guid g = Iface; uint len;
        if (CM_Get_Device_Interface_List_SizeW(out len, ref g, null, 0) != 0 || len <= 1) return new string[0];
        char[] buf = new char[len];
        if (CM_Get_Device_Interface_ListW(ref g, null, buf, len, 0) != 0) return new string[0];
        return new string(buf).Split(new[] { '\0' }, StringSplitOptions.RemoveEmptyEntries);
    }

    public static byte[] Query(string path, uint code, int size) {
        using (SafeFileHandle h = CreateFileW(path, 0x80000000 /*GENERIC_READ*/, 3, IntPtr.Zero, 3, 0, IntPtr.Zero)) {
            if (h.IsInvalid) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            byte[] buf = new byte[size]; uint got;
            if (!DeviceIoControl(h, code, IntPtr.Zero, 0, buf, (uint)size, out got, IntPtr.Zero))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            Array.Resize(ref buf, (int)got);
            return buf;
        }
    }
}
}
'@
}

$codecNames = @{ 0 = 'PCM'; 1 = 'SBC'; 2 = 'AAC'; 3 = 'aptX'; 4 = 'LDAC' }
$eventNames = @{ 0 = 'None'; 1 = 'AvdtpState'; 2 = 'MediaStart'; 3 = 'MediaStop'; 4 = 'AbrStep';
                 5 = 'TxCritical'; 6 = 'Connect'; 7 = 'Error' }
$avdtpStates = @('Idle', 'WaitDiscover', 'WaitGetCaps', 'WaitSetConfig', 'WaitOpen', 'WaitStart',
                 'Streaming', 'WaitSuspend', 'OpenPaused', 'WaitClose', 'WaitAbort', 'Closed', 'Failed',
                 'Configured', 'OpenWaitMedia')
# SONATINA_STATE field order (public.h)
$stateFields = @('Size', 'Codec', 'BitrateKbps', 'Opened', 'Packets', 'Errors', 'Underruns',
                 'TxTimeUs', 'DelayMs', 'NegotiatedSampling', 'NegotiatedChannels', 'CurrentBitpool',
                 'TxTimeMaxUs', 'TxTimeAvgUs', 'TxTimeHighCount', 'TxTimeCriticalCount',
                 'AbrAdjustments', 'TxFlushTO')

function Show-Device([string]$path) {
    $addr = [Sonatina.Ctl]::Query($path, [Sonatina.Ctl]::IOCTL_GET_ADDRESS, 64)
    $hex = [Text.Encoding]::ASCII.GetString($addr, 16, 12)
    $b = [Sonatina.Ctl]::Query($path, [Sonatina.Ctl]::IOCTL_GET_STATS, 4096)

    $st = [ordered]@{}
    for ($i = 0; $i -lt $stateFields.Count; $i++) {
        $st[$stateFields[$i]] = [BitConverter]::ToUInt32($b, 4 + 4 * $i)
    }
    $total = [BitConverter]::ToUInt32($b, 76)
    $count = [BitConverter]::ToUInt32($b, 80)

    Write-Host ("== Headset {0}" -f $hex) -ForegroundColor Cyan
    Write-Host ("   streaming={0}  codec={1}  {2} Hz x{3}  {4} kbit/s  bitpool={5}" -f
        [bool]$st.Opened, $codecNames[[int]$st.Codec], $st.NegotiatedSampling, $st.NegotiatedChannels,
        $st.BitrateKbps, $st.CurrentBitpool)
    Write-Host ("   packets={0}  errors={1}  underruns={2}  tx avg/max={3}/{4} us  delay={5} ms  ABR steps={6}" -f
        $st.Packets, $st.Errors, $st.Underruns, $st.TxTimeAvgUs, $st.TxTimeMaxUs, $st.DelayMs, $st.AbrAdjustments)
    Write-Host ("   last {0} of {1} events:" -f $count, $total)
    for ($i = 0; $i -lt $count; $i++) {
        $o = 88 + 24 * $i   # SONATINA_TRACE_EVENT: ULONGLONG + 3 x ULONG, 8-byte aligned
        $ts = [BitConverter]::ToUInt64($b, $o) / 1e6
        $kind = [int][BitConverter]::ToUInt32($b, $o + 8)
        $a0 = [BitConverter]::ToUInt32($b, $o + 12)
        $a1 = [BitConverter]::ToUInt32($b, $o + 16)
        $detail = switch ($kind) {
            1 { "state={0} remoteSEID={1}" -f $(if ($a0 -lt $avdtpStates.Count) { $avdtpStates[$a0] } else { $a0 }), $a1 }
            2 { "rate={0} mtu={1}" -f $a0, $a1 }
            3 { "codec={0} underruns={1}" -f $a0, $a1 }
            4 { "{0} -> {1}" -f $a0, $a1 }
            5 { "tx={0} us status=0x{1:X8}" -f $a0, $a1 }
            6 { "{0} attempt={1}" -f $(if ($a0 -eq 1) { 'headset connected to us' } else { 'we connected out' }), $a1 }
            7 { "errors={0}" -f $a0 }
            default { "{0} {1}" -f $a0, $a1 }
        }
        Write-Host ("   {0,12:N3}s  {1,-11} {2}" -f $ts, $eventNames[$kind], $detail)
    }
}

do {
    if ($Watch) { Clear-Host }
    $paths = [Sonatina.Ctl]::Interfaces()
    if ($paths.Count -eq 0) {
        Write-Warning 'SonatinaAudio is not loaded on any device (driver not installed, or headset not paired).'
    }
    foreach ($p in $paths) {
        try { Show-Device $p } catch { Write-Warning ("{0}: {1}" -f $p, $_.Exception.Message) }
    }
    if ($Watch) { Start-Sleep -Seconds 1 }
} while ($Watch)
