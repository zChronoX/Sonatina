# Sonatina

A small, self-contained Bluetooth audio stack in pure Python: a **source**, a
**headset simulator** (sink), a settings **service** and a Tkinter **GUI**. There
is no kernel driver and no Bluetooth adapter requirement: the data plane runs on
UDP loopback, so the whole handshake and media path can be exercised and measured
on a single machine.

The signalling is a reduced AVDTP subset (Discover, GetCapabilities,
SetConfiguration, Open, Start, Suspend, Close, Abort) and the media framing is
RTP with real SBC/AAC/aptX payload layouts. The registry holds one profile per
device plus a live telemetry block.

## Requirements

- Python 3.11+ and `numpy`.
- Optional: `soundcard` for WASAPI loopback capture (`uv sync --extra loopback`).
- Optional: FFmpeg on `PATH` for AAC and aptX bitstreams. Without it the stack
  still runs PCM and the SBC framing path.

## Install

```powershell
cd sonatina
uv sync --extra dev
```

or, without `uv`:

```powershell
python -m pip install numpy
```

The package is flat: run it as a module from the repository root.

## Quick start

```powershell
# 1) headset simulator, waits up to 15 s for a source
python -m sonatina sink --caps pcm,sbc --wait 15 --duration 8 --out out.wav

# 2) in a second shell: Bluetooth audio source for 5 s
python -m sonatina source --device 001122AABBCC --codec sbc --duration 5

# 3) one-shot demo (sink in background + source, verifies the WAV)
powershell -ExecutionPolicy Bypass -File scripts\run-demo.ps1 -Seconds 6
```

A successful run prints, on the sink side, a line like:

```text
sink: codec=sbc packets=500 lost=0 errors=0 bytes=189500 bitrate_kbps=379.0 ...
```

and writes `out.wav`.

## Commands

```text
python -m sonatina devices   [--json] [--hive HKCU|HKLM] [--root PATH]
python -m sonatina profile   list|dump|get|delete|set ...
python -m sonatina sink      --caps pcm,sbc --wait 15 --duration 8 --out out.wav
python -m sonatina source    --device 001122AABBCC --codec sbc --duration 5
python -m sonatina svc       [--console] [--debounce 1000] [--cooldown 20000]
python -m sonatina gui       [--selftest]
python -m sonatina bench     --codec sbc --bitpool 24,53
```

### `profile set`

```powershell
python -m sonatina profile set --device 001122AABBCC --codec sbc `
  --sbc-sampling 48000 --sbc-channel-mode joint --sbc-max-bitpool 53
```

Writing a profile bumps `Control\Trigger`, which the service watches. Use
`--no-trigger` to skip that.

### `sink` and `source`

`--caps` is the list of codecs the simulated headset advertises. If the source
asks for a codec the sink does not offer, the handshake is rejected and the
source exits with code `3`.

The source can take its configuration from the registry:

```powershell
python -m sonatina source --device 001122AABBCC --profile-from-registry
```

PCM input can be `sine` (default), a WAV file, or live WASAPI loopback
(`--pcm loopback`, requires `soundcard`).

## Registry layout

All keys live under `HKCU\Software\Sonatina` by default (`--hklm` moves them to
`HKLM`, which needs administrator rights):

```text
Devices\<BD_ADDR>\
    Codec, SbcChannelMode, SbcSamplingFrequency, SbcSubbands,
    SbcBlockLength, SbcAllocationMethod, SbcMinimumBitpool,
    SbcMaximumBitpool, AacChannelMode, AacSamplingFrequency, AacBitrate
    Capability\     written by the sink after the handshake
    Current\        written by the source at 1 Hz (codec, bitrate, packets, ...)
Control\
    Trigger         incremented by the UI or CLI to wake the service
```

## Service

`python -m sonatina svc` waits on a registry notification
(`RegNotifyChangeKeyValue`, asynchronous), debounces bursts of changes and
enforces a 20 s cooldown between two apply cycles. `--duration N` stops it after
N seconds; `--once` applies the current configuration and exits.

To start it at logon:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
# remove it with: -Remove
```

## Tests

```powershell
python -m unittest discover -s tests -v
```

The suite covers the SBC model and capability bytes, RTP header and payload
framing, AVDTP messages and the sink state machine, the registry store
(under a temporary `Software\Sonatina-Test` key), device-address parsing and the
debounce/cooldown policy.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | unexpected error (e.g. registry access) |
| 2 | invalid arguments or configuration |
| 3 | codec not supported by the sink |
| 4 | no device / no session |
| 5 | encoder or decoder unavailable (FFmpeg missing, bad stream) |

## Limitations

- No kernel driver: the Windows audio endpoint is not replaced, so a real
  headset cannot be switched from SBC to AAC by this project alone.
- SBC is carried with the correct frame length and bitrate model but the payload
  is a synthetic carrier, not a real SBC bitstream (that needs libsbc/AOSP).
  AAC and aptX are real bitstreams produced by FFmpeg.
- The L2CAP transport is experimental and conflicts with the Windows Bluetooth audio stack,
  which normally owns the connection; UDP loopback is the supported default.
- LDAC is not implemented.
