# Sonatina native driver

Scaffold for the Windows kernel driver that replaces the inbox Bluetooth audio function
driver on the Bluetooth *AudioSink* devnode. It is a KS/PortCls audio driver with
a BRB-based L2CAP transport and an AVDTP/RTP stack.

**Status: not built, not signed, not tested.** This machine has no Visual Studio,
no WDK and no signing tools. See `DESIGN.md` for the full plan and §5 there for
the build/sign/test procedure.

## Layout

```
driver/
  DESIGN.md                  architecture, gap analysis, milestones
  INTEGRATION.md             how to vendor SysVAD + AOSP SBC and wire it up
  README.md                  this file
  build.ps1                  environment check + msbuild
  include/public.h           shared kernel/user IOCTL and structs
  inf/SonatinaAudio.inf      installation package (scaffold)
  src/device.h               device extension
  src/driver.c               DriverEntry / AddDevice / StartDevice / IOCTLs
  src/brb_transport.h/.c     BTHDDI interface + L2CAP open/send/close
  src/avdtp.h/.c             AVDTP messages and capabilities (portable C)
  src/rtp.h/.c               RTP header and SBC payload (portable C)
  src/codec_sbc.h/.c         SBC maths + libsbc wrapper
```

Still to add with the toolchain (milestones D2–D4):

```
  src/miniport_wavert.cpp    WaveRT render miniport (from SysVAD)
  src/miniport_topology.cpp  topology miniport (from SysVAD)
  src/stream.c               encode + packetize + send thread
  SonatinaAudio.vcxproj      MSBuild project
```

## Prerequisites

- Visual Studio 2022 Build Tools (Desktop C++), Windows SDK 10, **WDK 10**.
- `msbuild`, `inf2cat`, `InfVerif`, `signtool`, `devcon`/`pnputil`.
- A test VM with a snapshot; `bcdedit /set testsigning on` unless the package is
  attested-signed.
- For distribution: EV code-signing certificate + Partner Center attestation.

## Safety

Never load a development build of this driver on a daily-use machine: a bug in
the stream thread bugchecks the system. Use a VM, Driver Verifier and small
increments.

## Next step

Milestone **D1**: driver loads on the devnode, device starts, endpoint
enumerates. It requires the WDK; the design and shared contracts are ready.
