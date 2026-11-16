# Sonatina — development roadmap

Single working document for everything still to build, why it is needed, and the
facts gathered from the static study that make the implementation easier.

This file lives in the workspace root on purpose: it is a planning/engineering
document, **not** part of the `sonatina/` deliverable. The project stays free of
references to the studied product; provenance is in `NOTICE.md`.

Legend: `[ ]` todo · `[~]` partial · `[x]` done · **Why** = reason, **Do** =
deliverable, **Done** = verifiable acceptance.

---

## 0. Where we are

| Area | State |
|---|---|
| Bluetooth audio reference stack (Python) | `[x]` 81+ unit tests green (incl. `test_reg` + LDAC AVDTP/RTP/codec), loopback demo exits 0 |
| SBC / LDAC / RTP / AVDTP logic | `[x]` in Python & portable C (`google/libsbc` + AOSP `libldac` fixed-point Apache-2.0 vendored) |
| Kernel driver design | `[x]` `sonatina/driver/DESIGN.md` |
| Driver scaffold (INF, contracts, BRB layer) | `[~]` compiles Debug/Release x64 & ARM64 + `InfVerif /w` green in CI (`#22`); VM load pending |
| Audio endpoints (WaveRT/topology) | `[~]` implemented from MS-PL SysVAD, compiles x64/ARM64 in CI; VM runtime pending |
| Real L2CAP / SBC + LDAC media + ABR | `[~]` AVDTP + `google/libsbc` + `libldac` + ABR/FlushTO compiled; T2/T4 UDP loopback verified; VM headset runtime pending |
| Service (native) & GUI (`SonatinaConfig.exe`) | `[~]` `SonatinaSvc.exe` + PyInstaller `SonatinaConfig.exe` built & smoke-tested; VM service start pending |
| Signing / installer | `[~]` `install-driver.ps1` (rollback), `uninstall-driver.ps1`, WiX v4 `Sonatina.wxs` verified; EV signing & VM matrix pending |

Definition of done for the whole programme: on a clean Windows 10/11 machine, a
signed `SonatinaAudio.sys` binds to a paired headset's AudioSink devnode, exposes
a render endpoint, streams SBC over real L2CAP, and a per-device profile change
reconnects the headset with the new parameters.

---

## 1. Phase A — foundations

### A1. Host test harness for the protocol C code `[x]`
- **Why**: `avdtp.c`, `rtp.c`, `codec_sbc.c` must produce byte-identical output
  to the Python reference. A host build catches errors without a VM or WDK.
- **Do**: `sonatina/driver/tests/host_test.c` (or a tiny CMake project) that
  compiles the three files with `-DSONATINA_HOST` and prints vectors; a Python
  script that generates the same vectors from `sonatina/`.
- **Done**: for 20 cases (SBC capability, AVDTP Discover/GetCaps/SetConfig, RTP
  header, SBC payload) the C and Python byte strings are equal.
- **Verified**: compiled with WinLibs `gcc 16.2.0` (`-Wall -Wextra -Werror -DSONATINA_HOST`)
  via `driver/tests/run_host_tests.ps1`; 28 byte vectors + 5 SBC math cases passed
  with 0 failures.

### A2. MSBuild project + package validation `[x]`
- **Why**: without a project file nothing compiles; without `InfVerif` the
  package may fail to install.
- **Do**: `SonatinaAudio.vcxproj` from the WDK "Kernel Mode Driver (WDM)"
  template, adding `src\*.c`, `inf\SonatinaAudio.inf`, `-DSONATINA_KERNEL`,
  `third_party/sbc` include path.
- **Done**: `driver\build.ps1 -Configuration Debug` exits 0; `inf2cat` and
  `InfVerif /w` report no errors.
- **Verified**: `msbuild sonatina\driver\SonatinaAudio.vcxproj /m /p:Configuration=Debug|Release /p:Platform=x64 /p:SignMode=Off`
  and `InfVerif.exe /w sonatina\driver\inf\SonatinaAudio.inf` pass with 0 warnings
  and 0 errors in GitHub Actions CI (`driver-compile-x64`, WDK 10.0.28000.0).
  `build.ps1` updated for local WDK builds (`vswhere` + `InfVerif` + `inf2cat`).

### A3. Safe test environment `[ ]`
- **Why**: a bug in the stream thread bugchecks the machine; testing on the
  daily PC is unacceptable.
- **Do**: Windows VM with snapshot, `bcdedit /set testsigning on`, Driver
  Verifier on `SonatinaAudio.sys`, WinDbg attached over a named pipe.
- **Done**: a deliberately broken build is caught by Verifier and the VM rolls
  back from the snapshot.

### A4. Logging and tracing `[~]`
- **Why**: kernel audio bugs are timing bugs; `DbgPrint` alone is not enough.
- **Do**: WPP/ETW tracing (`LogPages`, `VerboseOn` in the service key), plus
  in-memory ring (`src/trace_ring.h/.c`, 32 events) for AVDTP/RTP/ABR events
  exposed through `IOCTL_SONATINA_GET_STATS` (`SONATINA_STATS`).
- **Done**: a 10-minute session produces a trace with packet counts, tx times
  and state transitions.
- **Status**: `SONT_TRACE_RING` + `IOCTL_SONATINA_GET_STATS` implemented and
  verified in host tests; WPP/ETW + VM session test pending.

### A5. Compile-only CI `[x]`
- **Why**: keep the driver buildable while the main work is elsewhere.
- **Do**: a non-blocking GitHub Actions/Windows job that restores the WDK and
  compiles (no signing, no install).
- **Verified**: `.github/workflows/driver-ci.yml` runs `python-and-host-tests`
  (77 Python tests + A1 C host tests) and `driver-compile-x64` (Debug + Release
  `msbuild` + `InfVerif /w` + artifact upload); run #5 (`650c4ce`) green.

### A6. Coding and review rules `[x]`
- **Why**: kernel code needs stricter rules than the Python package.
- **Do**: `driver/CONTRIBUTING.md`: IRQL rules, no blocking in DPC, all
  allocations tagged, no floating point outside `KeSaveExtendedProcessorState`,
  no unbounded queues, every `IoCallDriver` with a completion routine.

---

## 2. Phase D — driver milestones

### D1. Driver loads on the AudioSink devnode `[~]`
- **Why**: the whole product depends on owning the same devnode the inbox Bluetooth audio
  driver owns.
- **Do**: `src/driver.c` (exists), `SonatinaAudio.vcxproj`, install script.
- **Done**: `devcon install` on the VM → device starts, no bugcheck, a control
  interface `{6E1F2C7A-…}` is openable from user mode.

### D2. WaveRT render + topology endpoints `[~]`
- **Why**: without KS endpoints the Windows audio engine cannot route audio to
  us and the headset never appears as a playback device.
- **Do**: vendor minimal `audio/sysvad` core (MS-PL: `LICENSE`, `NOTICE.txt`,
  `NewDelete.h/.cpp`, `simple.h`) into `third_party/sysvad/` and implement
  `CMiniportWaveRTSonatina` / `CMiniportWaveRTStreamSonatina`
  (`src/miniport_wavert.*`) + `CMiniportTopologySonatina`
  (`src/miniport_topology.*`); register `Wave` and `Topology` subdevices and
  physical connection via `SontRegisterAudioSubdevices` in `SontStartDevice`.
- **Done**: "Sonatina Bluetooth audio Wave" appears in Sound settings; KS Studio shows one
  render pin; 30 min of silence under Driver Verifier with no underrun errors.
- **Status**: compiles and links cleanly (`0 warnings`, `0 errors`) in Debug &
  Release x64 WDK CI (run #8 `c3b6513`); added `KSCATEGORY_REALTIME` /
  `PKEY_AudioEndpoint_Supports_EventDriven_Mode` to `SonatinaAudio.inf` and
  `IRP_MN_REMOVE_DEVICE` teardown in `driver.c`. VM runtime verification (A3)
  pending.

### D3. BRB transport + AVDTP handshake `[~]`
- **Why**: this is the real connection to the headset; everything else is local.
- **Do**: `src/brb_transport.c` (added cancellable `SontTransportReceive` via `BRB_L2CA_ACL_TRANSFER` IN + `StopEvent`/timeout) + portable AVDTP initiator session engine (`src/avdtp.c` SEP list / `SontAvdtpSelectSbcConfig` + `src/avdtp_session.h/.c`: `Discover` → `GetCapabilities` → `SetConfiguration` → `Open` → `Start`, plus `Suspend`/`Resume`/`Close`/`Abort`) + PASSIVE_LEVEL `SontSignalingWorker` in `driver.c` opening PSM `0x0019` and PSM `0x0017` and updating `ext->State` cleanly on completion / tearing down safely on `IRP_MN_REMOVE_DEVICE`.
- **Done**: against a real headset the driver logs the SEP list and the chosen
  SBC configuration; the bytes are identical to the Python reference.
- **Status**: portable C session state machine, 36 parity vectors + host tests verified vs `sonatina/avdtp.py`, and kernel D3 worker verified compiling cleanly in WDK CI (run #10 `ea2d3e3` e run #14 `e679e4c`); VM runtime verification (A3) pending.

### D4. SBC media path `[~]`
- **Why**: first audible, measurable parity point.
- **Do**: Apache-2.0 `google/libsbc` vendored in `third_party/sbc/` (`SONATINA_WITH_LIBSBC`), `src/codec_sbc.*` + `src/media_stream.*` (`SONT_PCM_RING` + `SONT_MEDIA_PACKETIZER`), WaveRT cyclic DMA tap (`SONT_PCM_SOURCE` in `miniport_wavert.cpp`), and PASSIVE_LEVEL `SontRunSbcMediaLoop` (`driver.c`) encoding PCM frames into RTP packets sent via `BRB_L2CA_ACL_TRANSFER` (`MediaChannel`, PSM `0x0017`).
- **Done**: 1 kHz tone audible; measured bitrate within 5% of
  `SontSbcBitrateKbps`; `Current\Bitrate` and `Current\Packets` advance at 1 Hz.
- **Status**: compiles and links cleanly (`0 warnings`, `0 errors`) in Debug & Release x64 WDK CI (run #14 `e679e4c`), real `google/libsbc` 1 kHz encode/decode round-trip + RTP packetizer host tests (`36 vectors + 5 math cases`, 0 failures) verified locally and in CI; VM runtime verification (A3) pending.

### D5. Service, profiles and reconnect `[~]`
- **Why**: users change codec/bitpool per headset; the change must apply.
- **Do**: native `SonatinaSvc.exe` (Win32 service) with the registry watcher,
  debounce and 20 s cooldown; profile write via `IOCTL_SONATINA_SET_PROFILE`;
  reconnect via `IOCTL_SONATINA_RECONNECT` and cycling the AVRCP devnode; initial
  per-device profile load in `SontStartDevice`.
- **Done**: `profile set --sbc-max-bitpool 35` → the headset reconnects and the
  wire bitpool is 35; two changes within 20 s apply once.
- **Status**: driver IOCTLs (`GET_ADDRESS`, `SET_PROFILE`, `RECONNECT`),
  registry boot-load, and native `SonatinaSvc.exe` implemented and compiled
  cleanly (`gcc -Wall -Wextra -Werror`); VM runtime verification (A3) pending.

### D6. ABR, flush timeout and latency `[~]`
- **Why**: the commercial value is stability on a weak radio ("no stuttering")
  and lower latency, not just a different codec.
- **Do**: measure per-packet tx time (`src/abr.h/.c`); short/long-term
  thresholds + clean recovery; dynamic bitpool updates (`SontSbcSetBitpool` /
  `SontMediaSetBitpool`); L2CAP media `FlushTO` (40 ms via
  `SontTransportOpenWithFlushTo`); expose counters in `SONATINA_STATE` and
  `Current\`.
- **Done**: with an artificially degraded link, audio does not drop out; the
  counters show ABR reacting; latency stays under the target.
- **Status**: portable ABR, dynamic SBC bitpool change, 40 ms media `FlushTO`,
  and registry telemetry sync implemented and host-tested; WDK CI + degraded-radio
  VM test pending.

### D7. Additional codecs `[~]`
- **Why**: parity beyond SBC. Technical work only; licensing decides the rest.
- **Do**: LDAC from AOSP Apache-2.0 (`[x]` vendored in fixed-point mode `_32BIT_FIXED_POINT` + AVDTP vendor ID `2D 01 00 00 / AA 00` + RTP framing + Python stack + T2 UDP loopback), AAC (needs a Fraunhofer FDK licence `[ ]`),
  aptX/HD/LL (needs a Qualcomm licence `[ ]`).
- **Done**: LDAC selectable, negotiated, host-encoded, and verified over T2 UDP loopback (`375 pkts, lost=0, errors=0, rms=0.573`); real headset runtime pending VM tests. AAC/aptX remain unimplemented due to commercial licensing.

### D8. Signing, installer and QA `[~]`
- **Why**: an unsigned kernel driver does not load with Secure Boot.
- **Do**: EV certificate, Partner Center attestation (or WHQL), MSI/WiX
  installer (`sonatina/installer/Sonatina.wxs`), rollback scripts (`install-driver.ps1` / `uninstall-driver.ps1`), ARM64 build.
- **Done**: ARM64 + x64 Debug/Release CI builds + `InfVerif /w` + `-DryRun` script checks passing; EV signing and VM install matrix (T5) pending.

---

## 3. Phase S — service and control plane

### S1. Native service skeleton `[~]`
- **Why**: the Python watcher proves the policy but cannot ship as a service.
- **Do**: Win32 service (`StartServiceCtrlDispatcherW`, `SERVICE_WIN32_OWN_PROCESS`,
  auto start) in `sonatina/service/service_main.c` (+ `--console` / `--once`).
- **Done**: installs, starts at boot, stops cleanly.

### S2. Watcher, debounce, cooldown `[~]`
- **Why**: a registry burst must produce one apply, not a restart storm.
- **Do**: `RegNotifyChangeKeyValue` (async, flags `0x10000004`), wait 5000 ms,
  debounce, cooldown 20000 ms (`service/apply_policy.c`).
- **Done**: unit test with an injected clock reproduces the Python tests
  (verified in `host_test.c`); manual: two quick changes → one apply, after 20 s → apply again.

### S3. Reconnect strategy `[~]`
- **Why**: parameters are read at (re)connection time.
- **Do**: cycle the AVRCP devnode (`CM_Disable_DevNode`, `Sleep(100)`,
  `CM_Enable_DevNode`) for the affected address, and call
  `IOCTL_SONATINA_SET_PROFILE` + `IOCTL_SONATINA_RECONNECT` (`service/device_ctrl.c`).
- **Done**: changing bitpool reconnects only the selected headset.

### S4. Trigger semantics `[~]`
- **Why**: UI → service signalling without polling.
- **Do**: `Control\Trigger` DWORD incremented by CLI/GUI; `SonatinaSvc.exe`
  watches `Control` and syncs `Current` telemetry from `IOCTL_SONATINA_GET_STATE`.
- **Done**: `profile set` bumps the trigger and the service applies within the
  debounce window.

### S5. Installer and rollback `[~]`
- **Why**: installing a function driver on a live devnode must be reversible.
- **Do**: `sonatina/scripts/install-driver.ps1` (staging + `pnputil` + `sc.exe` +
  automatic rollback on failure), `uninstall-driver.ps1`, and WiX v4 MSI
  manifest `sonatina/installer/Sonatina.wxs`.
- **Done**: `-DryRun` checks pass locally and in CI; live install→uninstall→
  reinstall matrix runs in VM (T5).

---

## 4. Phase G — GUI

### G1. Keep Tkinter as the control panel `[x]`
- **Why**: the existing GUI already speaks the registry contract; rewriting it
  now adds risk without user value.
- **Do**: point it at `HKLM\SYSTEM\CurrentControlSet\Services\SonatinaAudio\
  Parameters` (`--driver` CLI shortcut in `cli.py`), read `Current` (including
  D6 ABR/latency fields in `gui.py`) for telemetry, write profiles.
- **Done**: device list, codec matrix, live bitrate/packets/ABR telemetry, Apply.

### G2. Package the GUI `[x]`
- **Why**: shipping a Python GUI is fine only if the runtime is packaged.
- **Do**: `sonatina/scripts/gui_entry.py` + `sonatina/scripts/build-gui.ps1`
  (PyInstaller one-file `SonatinaConfig.exe` + `-CheckOnly` selftest).
- **Done**: `-CheckOnly` selftest green locally and in CI; standalone
  `sonatina/dist/SonatinaConfig.exe` built with PyInstaller and `--selftest`
  smoke-test verified locally (exit 0).

---

## 5. Phase T — testing and quality

### T1. Byte-equality oracle `[x]`
- **Why**: the Python stack is the executable specification.
- **Do**: generate vectors from `sonatina/`, assert equality in the C host test.
- **Done**: identical bytes for all AVDTP/RTP/SBC/LDAC cases (`39 vectors + 5 math cases, 0 failures`).

### T2. Loopback before hardware `[x]`
- **Why**: debug the driver state machine and media/ABR stack without a headset.
- **Do**: `sonatina/driver/tests/loopback_e2e.c` + `run_loopback_e2e.ps1`
  driving `avdtp_session.c`, `codec_sbc.c` (`google/libsbc`), `codec_ldac.c`
  (AOSP `libldac`), `media_stream.c`, `abr.c`, and `trace_ring.c` over UDP
  directly against `python -m sonatina sink`.
- **Done**: handshake + SBC media + `-SimulateCongestion` ABR step-down + LDAC
  media (`990 kbps`) succeed with `lost=0`, `errors=0`, and `RMS > 0.3`.

### T3. Driver Verifier + soak `[ ]`
- **Why**: catch pool, IRQL and lifetime bugs before users do.
- **Do**: 8 h soak with Verifier (special pool, IRQL, deadlock detection), radio
  loss, headset power-off, suspend/resume.
- **Done**: zero bugchecks, zero leaked pools, clean state after each scenario.

### T4. Quality metrics `[x]`
- **Why**: parity is measurable, not a feeling.
- **Do**: `sonatina/driver/tests/quality_report.py` + `tests/test_quality_report.py`
  sweeping SBC bitpools (`24`, `35`, `53`, plus `-SimulateCongestion`) and
  emitting a Markdown/JSON table (`tx_avg_us`, `tx_max_us`, `measured_kbps`,
  `expected_kbps`, `wire_kbps`, `abr_steps`, `lost`, `errors`, `decoded_rms`).
- **Done**: 4-cell table verified locally (`lost=0`, `errors=0`, `rms=0.345`)
  and wired into CI.

### T5. Install/uninstall/upgrade matrix `[ ]`
- **Why**: driver packages fail at lifecycle edges more than at runtime.
- **Do**: Win10 22H2, Win11 23H2/24H2, x64 + ARM64; upgrade over an older
  version; rollback after a failed install.
- **Done**: all cells green.

---

## 6. Reference facts — facts you can build on

Confidence labels: **Observed** = read directly in the package; **Derived** =
concluded from disassembly/structures; **Unknown** = not settled.

### 6.1 Package and binding
- **Observed** `AltBluetooth audio.sys` is a KS/PortCls **function driver** for
  `BTHENUM\{0000110b-0000-1000-8000-00805f9b34fb}` (AudioSink). INF includes
  `ks.inf,wdmaudio.inf`, `Needs=KS.Registration,WDMAUDIO.Registration`.
- **Observed** endpoints: `KSCATEGORY_AUDIO`+`KSCATEGORY_TOPOLOGY` (Topology)
  and `KSCATEGORY_AUDIO`+`KSCATEGORY_RENDER` (Wave), proxied by
  `{17cca71b-ecd7-11d0-b908-00a0c9223196}`.
- **Observed** kernel service: `SERVICE_KERNEL_DRIVER`, `SERVICE_DEMAND_START`,
  `SERVICE_ERROR_NORMAL`, binary in `%13%` (DriverStore).
- **Observed** no `bthport`/`bthprops` imports; it uses the parent bus. The
  documented mechanism is BTHDDI BRBs (see §7).

### 6.2 Control interface
- **Observed** private GUID `{F3A7ED1D-2D33-4733-AF84-279F8ADF2D13}` shared by
  driver, service and GUI (driver `.rdata` `0x140099FE0`).
- **Observed** `IOCTL 0x2F0003`, input 24 B, output 12 B, used by the service.
- **Unknown** the exact layout of those 24/12 bytes.
- **Our choice**: independent GUID `{6E1F2C7A-9B4D-4E6A-8C31-2F5B7D9A1E42}`
  and our own IOCTLs (`public.h`). We do not need to match the proprietary ABI.

### 6.3 Registry contract (Observed names)
Profiles under
`HKLM\SYSTEM\CurrentControlSet\Services\<service>\Parameters\Devices\<16 hex>`
with `Capability` and `Current` subkeys. Value families:

- SBC: `SbcChannelMode`, `SbcSamplingFrequency`, `SbcAllocationMethod`,
  `SbcSubbands`, `SbcBlockLength`, `SbcMinimumBitpool`, `SbcMaximumBitpool`.
- AAC: `AacChannelMode`, `AacSamplingFrequency`, `AacBitrateMode`, `AacBitrate`,
  `AacPeakBitrate`, `AacBandwidth`, `AacAfterBurner`, `AacMaxSize`.
- LDAC: `LdacChannelMode`, `LdacSamplingFrequency`, `LdacSampleFormat`,
  `LdacEqmid`.
- aptX: `AptxChannelMode`, `AptxSamplingFrequency`, `AptxHd*`, `AptxLl*`.
- Capability masks: `Sbc*Mask`, `Aac*Mask`, `Ldac*Mask`, `Aptx*Mask`.
- `Current` telemetry: `Opened`, `Bitrate`, `ScoActive`, `Delay`, `Error`,
  `TxTimeStats`, `TxTimeMax`, `TxFlushTO`, `LlFlushTO`, `TxLatency`,
  `LlLatency`, `TxSendAhead`, buffer thresholds, `TxPeriod`, `TxMaxSize`,
  `TxMaxDuration`, `LlTCL`, `LlICL`, `LlGWBL`, `LlBufferLevel`.
- Behaviour: `UseAvdtpReconfigureCmd`, `AutoConnectDelay`, `ReconnectDelay`,
  `DisableFormatChangeEvent`, `AvctpConnectionCheck`, `FormFactor`,
  `DisableGetAllCapabilities`, `DiscoverDelayIn`, `DiscoverDelayOut`,
  `SnkDiscoverTO`, `VolumeLevel`, `VolumeInitializeDelay`.
- ABR: `AbrEnable`, `AbrShortTermHighThreshold`, `AbrShortTermLowThreshold`,
  `AbrLongTermHighThreshold`, `AbrLongTermLowThreshold`, `AbrLongTermPeriod`,
  `AbrLongTermHighPct`, `AbrLongTermLowPct`, `AbrBufferHighShortTermAction`.
- Latency/retransmission: `TxTimeHighDuration`, `TxTimeHighCount`,
  `TxTimeCriticalDuration`, `TxTimeCriticalCount`.

Our schema reuses the SBC/AAC families and the `Current` idea; it does not need
the proprietary tuning values to reach functional parity.

### 6.4 Service behaviour (Derived from disassembly)
- `RegNotifyChangeKeyValue(hKey, TRUE, 0x10000004, hEvent, TRUE)`, where
  `0x10000004 = REG_NOTIFY_CHANGE_LAST_SET | REG_NOTIFY_THREAD_AGNOSTIC`.
- `WaitForMultipleObjects(3, …, 0x1388)` → 5000 ms.
- Debounce timer, then cooldown `0x4E20` → 20000 ms.
- Action: enumerate `BTHENUM\{0000110E-0000-1000-8000-00805F9B34FB}` (AVRCP),
  map address → config, then `CM_Disable_DevNode` + `Sleep(100)` +
  `CM_Enable_DevNode`.
- Separate helper: `CreateFileW` on the private interface and
  `DeviceIoControl(0x2F0003)`.

### 6.5 Open gaps (from the original study)
1. Layout of the 24 B / 12 B private IOCTL.
2. Exact composition of the device registry path (`Devices\Current`).
3. Which AVDTP features are implemented (retransmission, delay reporting,
   header compression) and how the channel is obtained from the PDO.
4. Exact ABR thresholds and the UI bitrate estimate formula.
5. Names/semantics of values written by the service in its `Parameters` key.
6. Where licence verification happens and its format.
7. ARM64 differences.

None of 1–7 blocks us: 3 is answered by the public BTHDDI API, 1/2/5 are
replaced by our own ABI, 4 is a tuning problem, 6 is not implemented, 7 is a
build target.

---

## 7. External references (public)

- BRB submission: `IOCTL_INTERNAL_BTH_SUBMIT_BRB`
  https://learn.microsoft.com/windows-hardware/drivers/ddi/bthioctl/ni-bthioctl-ioctl_internal_bth_submit_brb
- L2CAP open channel: `_BRB_L2CA_OPEN_CHANNEL`
  https://learn.microsoft.com/windows-hardware/drivers/ddi/bthddi/ns-bthddi-_brb_l2ca_open_channel
- Bluetooth driver stack and BRB list
  https://learn.microsoft.com/windows-hardware/drivers/bluetooth/using-the-bluetooth-driver-stack
- Building and sending a BRB
  https://learn.microsoft.com/windows-hardware/drivers/bluetooth/building-and-sending-a-brb
- PortCls introduction
  https://learn.microsoft.com/windows-hardware/drivers/audio/introduction-to-port-class
- WaveRT miniport
  https://learn.microsoft.com/windows-hardware/drivers/audio/developing-a-wavert-miniport-driver
- SysVAD sample
  https://github.com/microsoft/Windows-driver-samples/tree/main/audio/sysvad
- Simple Audio Sample
  https://learn.microsoft.com/samples/microsoft/windows-driver-samples/simple-audio-sample-device-driver/
- AOSP SBC encoder (Apache-2.0)
  https://android.googlesource.com/platform/external/bluetooth/sbc/
- the audio profile specification / AVDTP specifications (Bluetooth SIG)

---

## 8. Codec and licensing decisions

| Codec | Source | Licence | Can we ship it? |
|---|---|---|---|
| SBC | AOSP `external/bluetooth/sbc` | Apache-2.0 | yes |
| LDAC | AOSP | Apache-2.0 | yes |
| AAC | Fraunhofer FDK or own | commercial | only with an agreement |
| aptX / HD / LL | Qualcomm | proprietary/NDA | only with an agreement |

Consequence: "100% parity" for AAC/aptX is a business decision. The engineering
plan targets SBC then LDAC first.

---

## 9. Build, sign and test procedure

```powershell
# prerequisites: VS 2022 Build Tools (C++), Windows SDK 10, WDK 10
cd sonatina\driver
powershell -ExecutionPolicy Bypass -File build.ps1 -Configuration Debug -Platform x64

# package and sign
inf2cat /driver:inf /os:10_X64
signtool sign /fd SHA256 /a /f <cert>.pfx inf\SonatinaAudio.cat

# VM only
bcdedit /set testsigning on        # reboot
devcon install inf\SonatinaAudio.inf "BTHENUM\{0000110b-0000-1000-8000-00805f9b34fb}"
```

Distribution: EV certificate + Partner Center attestation (or WHQL).

---

## 10. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Kernel bugcheck | machine down | VM + snapshot, Verifier, small increments |
| Connection ownership fight | no audio | function driver replaces the inbox driver on the devnode; do not open L2CAP while the inbox driver owns it |
| Codec licensing | no AAC/aptX | legal decision before D7 |
| Signing cost/lead time | cannot install | start attestation early |
| Audio latency | product miss | measure from D4, tune in D6 |
| Endpoint format resampling | quality loss | negotiate the headset format, resample only when needed |

---

## 11. Clean-room rules

1. Implement from Microsoft docs and public AVDTP/RTP specifications.
2. Use the study above only for **interface facts** (binding, endpoints,
   registry value names, service side effects). Never transcribe disassembly
   into source.
3. OSS components keep their licence and attribution (SysVAD MIT, AOSP SBC/LDAC
   Apache-2.0).
4. No strings, icons, binaries or resources from the studied package.
5. `NOTICE.md` remains the separate provenance record.

---

## 12. Immediate next three actions

1. **A1 + A2**: host test harness, then create the WDK project so the scaffold
   compiles. This is the smallest step that turns code into something runnable.
2. **D2**: vendor SysVAD and register the two subdevices; get the endpoint to
   appear in Sound settings.
3. **D3**: hit a real headset with Discover/GetCapabilities and compare the
   bytes with `sonatina/`.
