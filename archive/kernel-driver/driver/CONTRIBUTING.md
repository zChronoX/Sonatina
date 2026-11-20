# Sonatina kernel driver — coding and review rules

Rules for all C/C++ code in `sonatina/driver/` (ROADMAP milestone **A6**).

## 1. Clean-room boundary
- Implement strictly from public Microsoft documentation (`bthddi.h`, PortCls,
  WaveRT, SysVAD) and public Bluetooth audio specifications (AVDTP, RTP, SBC, LDAC).
- Never copy code, strings, icons, or binary constants from commercial drivers
  or disassembly.
- Third-party OSS components (`third_party/sysvad` MIT, `third_party/sbc`
  Apache-2.0) must keep their original headers and licence files intact.

## 2. IRQL and synchronization
- Annotate or document expected IRQL on non-trivial functions (`PASSIVE_LEVEL` vs
  `<= DISPATCH_LEVEL`).
- Never block, wait (`KeWaitForSingleObject` with non-zero timeout), allocate
  paged memory, or call pageable functions at `DISPATCH_LEVEL` or inside a DPC.
- Keep spinlock critical sections tiny; never call `IoCallDriver` or
  `BthAllocateBrb` while holding a spinlock.

## 3. Memory and allocations
- Every kernel pool allocation must use `ExAllocatePool2` with pool tag `'anOS'`
  (`SONT_POOL_TAG`) and check for `NULL`.
- Never use unbounded queues or unbounded buffers for PCM/RTP packets; drop or
  apply backpressure with explicit limits and increment error/drop counters.
- PortCls owns the first `PORT_CLASS_DEVICE_EXTENSION_SIZE` bytes of
  `DeviceObject->DeviceExtension`. Always access custom state through
  `SONT_GET_EXTENSION(device)`.

## 4. Floating point
- Never use floating point (`float`/`double`) in kernel mode (`SONATINA_KERNEL`)
  outside a `KeSaveExtendedProcessorState` / `KeRestoreExtendedProcessorState`
  block. Keep `SontSbcBitrateKbps` integer-only in kernel paths or restrict
  floating-point helpers to host builds.

## 5. I/O and BRBs
- Every `IoCallDriver` call on a driver-allocated IRP must attach a completion
  routine (`IoSetCompletionRoutine`) and handle `STATUS_PENDING` cleanly.
- Never call `IoCompleteRequest` on `irp` inside `StartDevice` (`PCPFNSTARTDEVICE`):
  PortCls owns and completes the PnP start IRP.
- Validate all user-mode IOCTL buffer lengths and pointers before reading or
  writing `irp->AssociatedIrp.SystemBuffer`.

## 6. Testing and safety
- Never load or test a kernel driver binary on a daily-use host OS.
- All driver testing runs inside an isolated Windows VM with snapshot rollback,
  `bcdedit /set testsigning on`, and Driver Verifier enabled on `SonatinaAudio.sys`.
- Protocol changes in `avdtp.c`, `rtp.c`, and `codec_sbc.c` must pass the host
  test harness (`driver/tests/run_host_tests.ps1`) with zero byte differences
  against `sonatina/`.
