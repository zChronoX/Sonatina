# Sonatina native driver — design

Status: **design + scaffold**. Nothing here has been compiled or loaded: this
machine has no Visual Studio, no WDK and no signing tools (see §8). The code is a
starting point, not a working driver.

Goal: replace the Windows Bluetooth audio function driver on the Bluetooth *AudioSink*
devnode, expose a real audio endpoint and stream to a headset with a selectable
codec — functional parity with commercial Bluetooth audio drivers.

---

## 1. Evidence base

The design uses the static study of the commercial package for **interface
facts only** (Observed/Derived in the static study), plus public
Microsoft and Bluetooth audio documentation for everything else. No code is copied.

| Fact | Source | Class |
|---|---|---|
| The product is a KS/PortCls function driver on `BTHENUM\{0000110b}` (AudioSink) | INF + imports, REPORT §3.1–3.2 | Observed |
| It exposes KSCATEGORY_AUDIO/TOPOLOGY and KSCATEGORY_AUDIO/RENDER (Wave) subdevices | INF `[..Interfaces]` | Observed |
| Codec + AVDTP + RTP run in kernel; no Bluetooth imports, talks to the parent bus | imports, REPORT §3.1 | Observed |
| A private device interface GUID and `IOCTL 0x2F0003` link service/GUI to the driver | REPORT §3.1 | Observed |
| Profiles live under `Services\...\Parameters\Devices\<addr>` with `Capability`/`Current` | strings, REPORT §3.4 | Observed |
| The service cycles the AVRCP devnode (`CM_Disable/Enable_DevNode`) after a config change | disassembly, REPORT §3.3 | Derived |
| How the kernel gets L2CAP | `bthddi.h` + `IOCTL_INTERNAL_BTH_SUBMIT_BRB` | Public docs |
| Audio endpoint model | PortCls / WaveRT, SysVAD sample | Public docs |

The one thing the study could not settle — "how the driver obtains the L2CAP
channel" — is answered by the public WDK: a profile driver queries the Bluetooth
stack for `GUID_BTHDDI_PROFILE_DRIVER_INTERFACE` and submits BRBs to its parent.
That is the path we implement.

---

## 2. Gap analysis

| Capability | Sonatina today (Python) | Required for parity | Gap |
|---|---|---|---|
| OS integration | none (user process) | KS function driver on the AudioSink devnode | **driver** |
| Audio endpoint | none | WaveRT render + topology subdevices | **driver** |
| Transport | UDP loopback | L2CAP via BRB (PSM `0x0019` for both signalling and media) | **driver** |
| AVDTP | reduced subset, loopback | same subset over L2CAP + stream endpoints | medium |
| RTP media | yes | yes, plus jitter buffer and clock drift | medium |
| SBC | framing + bitrate model, synthetic payload | real libsbc encoder/decoder | **codec** |
| AAC | FFmpeg subprocess | in-kernel encoder (licence) | **blocked** |
| aptX / aptX HD / LL | flag only | Qualcomm proprietary encoder | **blocked** |
| LDAC | none | AOSP LDAC encoder (Apache-2.0) | large |
| Config | registry `HKCU\Software\Sonatina` | `HKLM\...\Services\SonatinaAudio\Parameters` | small |
| Service | Python watcher + debounce/cooldown | same, native exe, devnode reconnect | medium |
| GUI | Tkinter | can stay Tkinter initially | none |
| Telemetry | `Current` at 1 Hz | same, from kernel | small |
| ABR / flush / latency | bitrate model only | tx-time measurement, thresholds, ABR | large |
| Signing | n/a | EV cert + Partner Center attestation | **external** |

---

## 3. Target architecture

```
apps ──► Windows audio engine ──► KS render pin
                                   │
                       SonatinaAudio.sys  (WDM + PortCls)
                       ├─ WaveRT miniport  (render endpoint)
                       ├─ Topology miniport
                       ├─ AVDTP state machine   ── BRB ──► BTHENUM parent ──► headset
                       ├─ RTP packetizer / jitter buffer
                       ├─ codec: SBC (libsbc), later LDAC/AAC
                       └─ ABR / flush-time control
                                   ▲
                                   │ private device interface + IOCTL
                       SonatinaSvc.exe (watcher, reconnect)
                                   ▲
                       SonatinaConfig (Tkinter, later native)
```

Deliberate choice: **WDM + PortCls**, as in the Microsoft audio samples, instead
of a KMDF hybrid. It is the best-documented path for a KS audio function driver
and keeps the clean-room line clear.

### 3.1 Binding

- Hardware ID: `BTHENUM\{0000110b-0000-1000-8000-00805f9b34fb}` (AudioSink).
- Class `MEDIA`, `Include = ks.inf,wdmaudio.inf`,
  `Needs = KS.Registration,WDMAUDIO.Registration`.
- Kernel service `SonatinaAudio`, `SERVICE_DEMAND_START`.
- Interfaces: `KSCATEGORY_AUDIO`+`KSCATEGORY_TOPOLOGY` (topology) and
  `KSCATEGORY_AUDIO`+`KSCATEGORY_RENDER` (wave), proxied by the standard KS proxy
  CLSID.

### 3.2 Transport (the core)

1. In `StartDevice`, get the parent PDO (`PcGetPhysicalDeviceObject`) and send
   `IRP_MN_QUERY_INTERFACE` for `GUID_BTHDDI_PROFILE_DRIVER_INTERFACE` down the
   parent stack. This yields `BthAllocateBrb`, `BthFreeBrb`, `BthInitializeBrb`,
   `BthReuseBrb`.
2. **Both AVDTP channels use PSM `0x0019`**: the first L2CAP channel is
   signalling, the second one (opened after `Open`) is the media transport.
   PSM `0x0017` is AVCTP (AVRCP) and never carries audio.
3. The worker registers an L2CAP server for the headset's own address
   (`BRB_L2CA_REGISTER_SERVER`, PSM `0x0019`). Headsets reconnect by themselves
   on power-on; the indication callback (≤ DISPATCH_LEVEL) only queues the
   request, and the worker answers it with `BRB_L2CA_OPEN_CHANNEL_RESPONSE` at
   PASSIVE_LEVEL.
4. Outgoing connections (`BRB_L2CA_OPEN_CHANNEL`) are bounded by a timeout and
   cancelled by `StopEvent`. Retry policy: a few attempts after start
   (2 s / 5 s / 10 s), then only while Windows is really playing audio, so a
   headset that is switched off is not paged forever.
5. A single asynchronous receive (`BRB_L2CA_ACL_TRANSFER` IN, reused IRP/BRB)
   stays posted on the signalling channel for the whole connection, so commands
   from the headset are answered while streaming.
6. `IndicationRemoteDisconnect` sets a flag and an event; the worker closes the
   handles (`BRB_L2CA_CLOSE_CHANNEL`) and reconnects.
7. The headset's receive MTU is read from the open/response results and used
   for packetization (we advertise a 1024-byte local MTU so the smaller
   negotiated value is always the headset's).

### 3.2.1 AVDTP roles (`avdtp_session.c`)

- **Initiator**: Discover → GetCapabilities on every sink SEP (preferred codec
  first, SBC kept as fallback, e.g. when LDAC is switched off in the headset
  app) → SetConfiguration → Open → *open the transport channel* → Start.
  Start is never sent before the transport channel exists.
- **Acceptor**: every command the headset may send gets an answer —
  Discover (our SBC SEP 1, LDAC SEP 2 when enabled), Get(All)Capabilities,
  SetConfiguration, GetConfiguration, Reconfigure (rejected), Open, Start,
  Suspend, Close, Abort, DelayReport; unknown signals get General Reject.
- Collisions: if the headset configures us before our own SetConfiguration is
  out, its choice wins and our handshake is abandoned; afterwards its
  SetConfiguration is rejected with `SEP_IN_USE`.
- After an incoming connection the driver waits 1 s for the headset to drive
  the setup before starting its own Discover.
- Wire format follows the specification bit layout (signal ID in bits 5..0 of
  octet 1, SEID in bits 7..2, In-Use bit 1, TSEP bit 3) and is pinned by golden
  bytes in the C and Python tests.

### 3.3 Audio path

- WaveRT render miniport; its timer DPC copies the cyclic buffer into a PCM
  ring (`SONT_PCM_SOURCE`).
- The media engine is paced by a QPC media clock (`SONT_MEDIA_CLOCK`, 20 ms
  send-ahead, burst cap) — not by PCM arrival. Each due codec frame pulls PCM
  through `SONT_PCM_PULL`: 30 ms jitter buffer, linear resampler and
  up/down-mix when the endpoint format differs from the negotiated codec
  format, latency bound (backlog > 150 ms is trimmed).
- When Windows plays nothing (or underruns) the headset receives **digital
  silence** and the stream stays started: no Suspend/Start cycles, which is
  where many headsets fall silent until power-cycled.
- If the headset suspends the stream (e.g. the phone takes over on multipoint),
  the driver resumes only when real audio (not an open silent stream) shows
  up in the tap, with exponential back-off when Start is rejected.
- Encode → packetize (RTP, timestamps in samples) → `BRB_L2CA_ACL_TRANSFER`
  with a bounded timeout; per-packet tx time feeds ABR (SBC bitpool, LDAC
  EQMID). Timer resolution is raised to 2 ms only while streaming.

### 3.4 Control plane

- Private device interface GUID `{6E1F2C7A-9B4D-4E6A-8C31-2F5B7D9A1E42}`.
- `IOCTL_SONATINA_GET_STATE`, `IOCTL_SONATINA_SET_PROFILE`,
  `IOCTL_SONATINA_GET_STATS` (see `include/public.h`).
- Profiles: `HKLM\SYSTEM\CurrentControlSet\Services\SonatinaAudio\Parameters\
  Devices\<16 hex digits>\` with the same value families Sonatina already uses
  (`Codec`, `Sbc*`, `Aac*`, `Capability`, `Current`, `Trigger`).
- Service: `RegNotifyChangeKeyValue` (async) → debounce → 20 s cooldown → apply.
  Apply = write config to the driver via IOCTL and cycle the Bluetooth audio/AVRCP devnode
  so the headset reconnects with the new parameters.

---

## 4. Codecs and licensing (non-technical blocker)

| Codec | Source | Licence | Parity |
|---|---|---|---|
| SBC | AOSP `external/bluetooth/sbc` | Apache-2.0 | yes, first target |
| LDAC | AOSP | Apache-2.0 | yes, later |
| AAC | Fraunhofer FDK or own | commercial licence for an encoder | **licence needed** |
| aptX / HD / LL | Qualcomm | proprietary, NDA + royalty | **not freely implementable** |

So "100%" parity is a product/legal decision as much as an engineering one.
Technically reachable without licences: SBC + LDAC. AAC and aptX need agreements
with their owners.

---

## 5. Build, sign, test

Required (not installed here):

- Visual Studio 2022 Build Tools (MSVC x64) + Windows SDK 10 + **WDK 10**.
- `msbuild` from the WDK integration; `signtool`, `inf2cat`, `devcon`/`pnputil`.
- For development: a **VM** with a snapshot, `bcdedit /set testsigning on`, and
  Driver Verifier. Never iterate a kernel driver on the daily machine.
- For distribution: EV code-signing certificate and Partner Center attestation
  (or WHQL) so the package loads with Secure Boot on.

Test ladder:

1. `devcon install` on the VM → device starts, endpoint appears in Sound settings.
2. Play silence → no underruns, no Verifier bugcheck for 30 min.
3. Real headset: Discover/GetCapabilities/SetConfiguration/Open/Start captured
   and compared with Sonatina's Python implementation (same bytes on the wire).
4. Audio: 1 kHz tone, measure bitrate/latency/loss; sweep bitpool.
5. Config change → service cycle → reconnect with new parameters.
6. Suspend/resume, headset power-off, radio loss, driver unload.
7. 8 h soak with Driver Verifier and ETW/WPP tracing.

---

## 6. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| D0 | project + INF + shared header | INF passes `inf2cat`/`InfVerif`; design reviewed |
| D1 | driver loads on the AudioSink devnode | device starts, no bugcheck, endpoint enumerates |
| D2 | WaveRT render + topology, silence | KS Studio sees the pins; 30 min idle clean |
| D3 | BRB transport: open signalling, Discover/GetCapabilities | real headset answers; bytes match the Python stack |
| D4 | SBC encode + media channel | audible audio, measured bitrate within 5% of the model |
| D5 | service + registry + per-device profile | codec/bitpool change applies after reconnect |
| D6 | ABR + flush timeout + latency tuning | stable under weak radio; no dropouts at target bitrate |
| D7 | LDAC (and AAC/aptX if licensed) | codec selectable and measured |
| D8 | signing + installer + QA matrix | installs on clean Win10/11 x64/ARM64 |

D0 is what exists today. D1 needs the toolchain.

---

## 7. Risks

- **Kernel stability**: a bug in the stream thread bugchecks the machine. VM +
  Verifier + small increments.
- **Connection ownership**: the inbox Bluetooth audio stack owns the devnode until our
  function driver replaces it; coexistence must be handled by the INF binding
  and the service's reconnect logic, not by fighting for L2CAP.
- **Codec licensing** (AAC, aptX): may cap parity below 100%.
- **Signing**: without attestation the driver will not load with Secure Boot.
- **Latency**: kernel encode + BRB round trips need careful buffering; aptX LL
  class latency is a product-level target, not a by-product.

---

## 8. Clean-room rules

1. Implement from Microsoft docs (`bthddi.h`, PortCls/WaveRT samples) and the
   public AVDTP/RTP specifications.
2. Use the static study only for **interface facts**: hardware ID, endpoint
   categories, registry value names, service side effects. Never transcribe
   disassembly into source.
3. OSS code (AOSP SBC/LDAC) is vendored with its licence and attribution.
4. No strings, icons, resources or binaries from the studied package.
5. Keep `NOTICE.md` (workspace root) as the separate provenance record.

---

## 9. What already exists and is reused

- AVDTP/RTP/SBC framing logic and the negotiation state machine in
  `sonatina/` (Python) are the **reference implementation** for the kernel
  version: same bytes on the wire, used as an oracle in tests.
- The registry schema and the service policy (debounce/cooldown) carry over.
- The GUI can stay Tkinter until the native endpoint works.
