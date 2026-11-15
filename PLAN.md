# Plan

Goal: a small, self-contained Python Bluetooth audio stack (`sonatina/`): per-device codec
profiles in the registry, a settings watcher service, an AVDTP/RTP data plane and
a GUI, verified end-to-end on UDP loopback.

The project is named **Sonatina** (package `sonatina`, CLI `python -m sonatina`,
registry root `HKCU\Software\Sonatina`). The separate provenance record is
`NOTICE.md` in the workspace root; it is not part of the project.

## T1 - Project skeleton and CLI
- Status: done
- Verification: `python -m sonatina --help` lists `devices profile sink source svc gui bench`; `devices` lists the Bluetooth audio devnodes.

## T2 - Registry profile store
- Status: done
- Verification: `python -m unittest tests.test_reg -v` passes; profiles round-trip under `HKCU\Software\Sonatina-Test` and the temporary key is removed.

## T3 - SBC model, RTP framing, AVDTP signalling
- Status: done
- Verification: `tests.test_sbc`, `tests.test_rtp`, `tests.test_avdtp` pass (SBC capability round-trip, bitrate monotonicity, RTP header round-trip, SBC/AAC payload framing, negotiation accept/reject).

## T4 - Headset simulator (sink)
- Status: done
- Verification: `python -m sonatina sink --caps pcm,sbc --duration 8 --out out.wav` negotiates and reports `codec=`, `packets>0`, `lost=0`.

## T5 - Source pipeline end-to-end (PCM/SBC)
- Status: done
- Verification: sink + `python -m sonatina source --device 001122AABBCC --codec sbc --duration 5`; both exit 0, sink reports `codec=sbc`, `lost=0`, WAV RMS > 0, `Current\Bitrate` written.

## T6 - Service with registry notification, debounce and cooldown
- Status: done
- Verification: `tests.test_debounce` passes; `python -m sonatina svc --once` logs one apply cycle and exits 0.

## T7 - GUI (Tkinter)
- Status: done
- Verification: `python -m sonatina gui --selftest` builds the window, runs one update cycle and exits 0.

## T8 - Negative paths
- Status: done
- Verification: sink `--caps sbc` + source `--codec aac` exits 3 with an explicit message; `bench --codec sbc --bitpool 24,53` prints 204.0 and 378.0 kbit/s, matching `sonatina.sbc.bitrate`.

## T9 - Docs and scripts
- Status: done
- Verification: `sonatina/README.md`, `scripts/run-demo.ps1`, `scripts/install-autostart.ps1` exist; `run-demo.ps1 -Seconds 6` exits 0 with `source_exit=0 sink_exit=0 wav_ok=True`.

## T10 - Provenance record
- Status: done
- Verification: `NOTICE.md` exists in the workspace root, is a single file and is not referenced by `sonatina/`. The project package contains no reference to the analysed product or to the research folder.

## Phase 2 - native driver (parity with commercial Bluetooth audio drivers)

Master backlog, rationale and the reference facts: **`ROADMAP.md`** in the
workspace root (planning document, not shipped with the project).

Goal: replace the inbox Bluetooth audio function driver on `BTHENUM\{0000110b}` with a
KS/PortCls driver (`SonatinaAudio.sys`) that exposes a real WaveRT render
endpoint, opens L2CAP through the public BTHDDI BRB interface and encodes the
selected codec in kernel. Design: `sonatina/driver/DESIGN.md`.

- **D0 - design + scaffold**: done. `DESIGN.md`, `driver/README.md`,
  `inf/SonatinaAudio.inf`, `include/public.h`. Not compiled: no WDK on this
  machine.
- **A1 - host test harness**: done. `driver/tests/host_test.c`,
  `driver/tests/generate_vectors.py`, `CMakeLists.txt`, `run_host_tests.ps1`,
  and `tests/test_driver_vectors.py`. Verified with WinLibs `gcc 16.2.0`
  (`-Wall -Wextra -Werror -DSONATINA_HOST`): 28 vectors + 5 math cases, 0 failures.
- **A2 - MSBuild project + build script**: done (via GitHub Actions WDK CI).
  `SonatinaAudio.vcxproj` (WDM template, `-DSONATINA_KERNEL`, `third_party\sbc`
  include, `FileDigestAlgorithm=sha256`, `Inf2Cat` local time) compiles
  `SonatinaAudio.sys` (Debug & Release x64, 0 warnings/errors) and
  `InfVerif /w sonatina\driver\inf\SonatinaAudio.inf` passes in CI run #5.
  `driver/build.ps1` ready for local WDK builds.
- **A5 - compile-only CI**: done. `.github/workflows/driver-ci.yml` (`windows-latest`
  + `Microsoft.Windows.WDK.x64` NuGet + Python/A1 host tests + `InfVerif /w`).
- **A6 - kernel coding rules**: done (`driver/CONTRIBUTING.md`).
- **D1 - driver loads**: partial (compiles and links cleanly in x64 WDK CI,
  runtime VM load pending). Fixed `PORT_CLASS_DEVICE_EXTENSION_SIZE`, `StartDevice`
  IRP ownership, `<ntifs.h>`/`<portcls.h>`/`<bthguid.h>`/`<initguid.h>` order,
  `PcGetPhysicalDeviceObject`, `L2CAP_CHANNEL_HANDLE *`, `portable.h` kernel
  typedefs. Acceptance: device starts on the AudioSink devnode in a VM, no
  bugcheck, control interface opens.
- **D2 - WaveRT + topology, silence**: partial (compiles and links cleanly in
  WDK CI run #8 `c3b6513`; VM runtime test pending). Added MS-PL
  `third_party/sysvad/` (`LICENSE`, `NOTICE.txt`, `NewDelete.*`, `simple.h`),
  `src/miniport_topology.*` (bridge + `KSPROPSETID_Jack` headphones),
  `src/miniport_wavert.*` (16-bit stereo 44.1-96 kHz render sink, `EX_TIMER` +
  integer QPC position/events), `src/miniport_subdevices.h`, wired into
  `SontStartDevice` / `SontPnpDispatch` (`driver.c`), `KSCATEGORY_REALTIME` /
  `Supports_EventDriven_Mode` (`SonatinaAudio.inf`) and `SonatinaAudio.vcxproj`.
  Acceptance: KS pins visible, 30 min idle clean under Driver Verifier.
- **D3 - BRB signalling**: partial (compiles and links cleanly in WDK CI run #10
  `ea2d3e3`; VM runtime verification A3 pending). Extended `src/avdtp.*`
  (SEP list, single-SEID, `SontAvdtpSelectSbcConfig`), added portable state
  machine `src/avdtp_session.h/.c` + 36 host/Python parity vectors
  (`generate_vectors.py`, `host_test.c`), added cancellable `SontTransportReceive`
  (`src/brb_transport.*`), and wired `SontSignalingWorker` (`driver.c`,
  `device.h`) opening PSM `0x0019` → handshake → PSM `0x0017` with `StopEvent`
  teardown on `IRP_MN_REMOVE_DEVICE`. Acceptance: wire bytes identical to the
  Python reference (verified in host tests) and handshake completes cleanly
  against a real headset in VM.
- **D4 - SBC media**: partial (compiles and links cleanly in Debug & Release x64
  WDK CI run #14 `e679e4c`; host tests passing; VM runtime verification A3
  pending). Vendored Apache-2.0 `google/libsbc` in `third_party/sbc/`, switched
  `src/codec_sbc.c` (`SONATINA_WITH_LIBSBC`), added `src/media_stream.h/.c`
  (`SONT_PCM_RING` + `SONT_MEDIA_PACKETIZER`), tapped WaveRT cyclic DMA bytes
  into `SONT_PCM_SOURCE` (`miniport_wavert.cpp`), and wired `SontRunSbcMediaLoop`
  (`driver.c`) -> `BRB_L2CA_ACL_TRANSFER` on `MediaChannel` (PSM `0x0017`).
  Acceptance: audible audio, bitrate within 5% of `sonatina.sbc.bitrate`.
- **D5 - service + profiles**: native service, registry
  `Services\SonatinaAudio\Parameters`, reconnect on change.
- **D6 - ABR/flush/latency**: tx-time measurement, thresholds, flush timeout.
- **D7 - LDAC (and AAC/aptX only with a licence)**: codec selectable.
- **D8 - signing + installer**: EV cert + attestation, install on clean
  Win10/11 x64/ARM64.

Blockers: WDK/Visual Studio absent; kernel work must run in a VM with test
signing; AAC and aptX need commercial licences and cannot be implemented freely.

## Notes
- Stack: Python 3.11+ flat package, stdlib + numpy; no build step. FFmpeg optional (AAC/aptX), `soundcard` optional (WASAPI loopback).
- Registry values: `Codec`, `Sbc*`, `Aac*`, `Capability`, `Current`, `Trigger`.
- Service constants: `RegNotifyChangeKeyValue(..., 0x10000004, ..., async)`, wait timeout 5000 ms, cooldown 20000 ms.
- Exit codes: 0 ok, 1 error, 2 args, 3 codec not supported by sink, 4 device/session missing, 5 encoder unavailable.
- Full suite: `cd sonatina; python -m unittest discover -s tests -v` (77 tests; 9 `test_reg` tests require HKCU registry write access outside the sandbox).
