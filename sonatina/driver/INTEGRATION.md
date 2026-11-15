# Integration guide

How to turn the scaffold into a working driver without writing PortCls boilerplate
from scratch. Everything external here is open source with a permissive licence.

## 1. Vendor the audio scaffolding

Use the Microsoft audio samples as the PortCls base (MIT licence):

- `audio/sysvad/EndpointsCommon` — WaveRT render miniport + topology miniport,
  the adapter/stream classes and the KS property handlers.

Copy it under `third_party/sysvad/`, keep its `LICENSE`, and subclass:

- `CMiniportWaveRTSonatina : CMiniportWaveRT` — exposes one render pin and pulls
  PCM from the cyclic buffer.
- `CMiniportTopologySonatina : CMiniportTopology` — minimal topology with the
  endpoint association property.

Do this instead of reimplementing `IMiniportWaveRT` by hand: the boilerplate is
large and security-sensitive, and the sample is the reference Microsoft tests
against.

## 2. Vendor the SBC encoder

Use AOSP `external/bluetooth/sbc` (Apache-2.0):

```
third_party/sbc/           # sbc.c, sbc.h, sbc_encoder.c ...
```

Add its include directory to the project and define `SONATINA_WITH_LIBSBC` for
`codec_sbc.c`. Without the define the file still compiles with a stub encoder
(useful for D1/D2 bring-up).

## 3. Module map

| File | Role | Portable? |
|---|---|---|
| `src/avdtp.c` | AVDTP messages and capabilities | yes |
| `src/rtp.c` | RTP header and SBC payload | yes |
| `src/codec_sbc.c` | SBC maths + libsbc wrapper | mostly |
| `src/brb_transport.c` | BTHDDI interface, L2CAP open/send/close | kernel only |
| `src/driver.c` | DriverEntry, AddDevice, StartDevice, IOCTLs | kernel only |

`avdtp.c` and `rtp.c` have no kernel dependency: build them into a host test too
and check the bytes against `sonatina/` (Python) on the same vectors.

## 4. Bring-up sequence (milestone D3)

1. `SontStartDevice` → `SontTransportInitialize`:
   `IRP_MN_QUERY_INTERFACE` for `GUID_BTHDDI_PROFILE_DRIVER_INTERFACE`, then
   `IOCTL_INTERNAL_BTHENUM_GET_DEVINFO` for the remote address.
2. `SontTransportOpen(&t, SONT_L2CAP_PSM_AVDTP, &ch)`.
3. Send `Discover`; parse the SEP list; for each SEP send `GetCapabilities`.
4. Pick the SBC SEP and send `SetConfiguration` (`SontAvdtpEncodeSetConfiguration`).
5. `SontTransportOpen(&t, SONT_L2CAP_PSM_MEDIA, &media)`.
6. Send `Open` then `Start` on the signalling channel.
7. Start the stream thread.

## 5. Stream thread (D4)

```c
/* one pass every ~20 ms, PASSIVE_LEVEL, one per active stream */
frames = SontSbcSamplesPerFrame(&params);
SontSbcEncode(encoder, pcm + consumed, frames,
              frame_buf + frame_off, sizeof(frame_buf) - frame_off, &written);
frame_off += written;
if (frame_count == frames_per_packet) {
    payload_len = SontRtpSbcPayload(frame_buf, frame_off, frame_count,
                                    payload, sizeof(payload));
    SontRtpEncodeHeader(&header, out);
    memcpy(out + SONT_RTP_HEADER_LEN, payload, payload_len);
    SontTransportSend(&t, media, out, SONT_RTP_HEADER_LEN + payload_len);
    header.sequence++;
    header.timestamp += frames_per_packet * samples_per_frame;
    frame_off = 0;
    frame_count = 0;
}
```

Take the PCM from the WaveRT cyclic buffer in the stream callback provided by the
miniport; do not block the audio engine.

## 6. Receive path

For disconnect handling and (later) delay reporting, post
`BRB_L2CA_ACL_TRANSFER` with `ACL_TRANSFER_DIRECTION_IN`, a preallocated buffer
and a completion callback that re-arms the request. Keep one receive BRB pending
per channel.

## 7. Build and sign

```powershell
# project: create from the WDK template "Kernel Mode Driver (WDM)", add src\*.c
powershell -ExecutionPolicy Bypass -File driver\build.ps1 -Configuration Debug

# package + catalog
inf2cat /driver:driver\inf /os:10_X64
signtool sign /fd SHA256 /a /f mycert.pfx driver\inf\SonatinaAudio.cat
```

On the test VM:

```powershell
bcdedit /set testsigning on      # reboot
devcon install driver\inf\SonatinaAudio.inf "BTHENUM\{0000110b-0000-1000-8000-00805f9b34fb}"
```

For distribution, sign through Partner Center attestation (or WHQL) with an EV
certificate.

## 8. Test ladder

1. Driver loads, device starts, no bugcheck (D1).
2. WaveRT endpoint appears; 30 min of silence under Driver Verifier (D2).
3. `Discover`/`GetCapabilities` against a real headset; compare bytes with the
   Python reference (D3).
4. 1 kHz tone: audible, bitrate within 5% of `SontSbcBitrateKbps` (D4).
5. Change bitpool in the registry → service cycles the device → new bitpool on
   the wire (D5).
6. Weak-radio soak: no dropouts at target bitrate (D6).

## 9. Intentionally missing

- WaveRT/topology miniport classes (take them from SysVAD).
- The stream and receive threads' scheduling code.
- The AVDTP state machine driver around `avdtp.c`.
- ABR/flush-time logic.
- AAC/aptX/LDAC.

These are the D2–D7 milestones in `DESIGN.md`.
