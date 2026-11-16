/* SonatinaAudio.sys - WDM/PortCls function driver for the Bluetooth AudioSink
 * devnode. This file is the scaffold: it initialises the BTHDDI transport and
 * the private control interface. WaveRT/topology subdevices and the streaming
 * thread are added in D2-D4 (see DESIGN.md).
 */
#include <ntifs.h>
#pragma warning(push)
#pragma warning(disable: 4115)
#include <portcls.h>
#pragma warning(pop)

#include <initguid.h>
#include "brb_transport.h"
#include "public.h"
#include "device.h"
#include "avdtp_session.h"
#include "miniport_subdevices.h"
#include "profile_validate.h"
#include "abr.h"
#include "codec_ldac.h"

#define SONT_POOL_TAG 'anOS'
#define SONT_AVDTP_RX_TIMEOUT_MS 5000u
#define SONT_AVDTP_MAX_STEPS     32u
#define SONT_AVDTP_MAX_IGNORED   8u
#define SONT_MEDIA_WAIT_MS       50u
#define SONT_MEDIA_L2CAP_MTU     679u
#define SONT_MEDIA_FLUSH_TO_MS   40u
#define SONT_MEDIA_MAX_PCM_CH    2u
#define SONT_MEDIA_MAX_SAMPLES   128u

typedef struct _SONT_SIGNALING_SCRATCH {
    SONT_AVDTP_SESSION session;
    SONT_MEDIA_PACKETIZER media;
    SONT_ABR abr;
    uint8_t tx[SONT_AVDTP_MAX_PDU];
    uint8_t rx[SONT_AVDTP_MAX_PDU];
    uint8_t rtp[1200];
    uint8_t ldac_frames[1024];
    int16_t pcm_frame[SONT_MEDIA_MAX_SAMPLES * SONT_MEDIA_MAX_PCM_CH];
    int16_t pcm_2x[SONT_MEDIA_MAX_SAMPLES * SONT_MEDIA_MAX_PCM_CH * 2];
} SONT_SIGNALING_SCRATCH, *PSONT_SIGNALING_SCRATCH;

static NTSTATUS SontAddDevice(PDRIVER_OBJECT driver, PDEVICE_OBJECT pdo);
static NTSTATUS SontStartDevice(PDEVICE_OBJECT device, PIRP irp, PRESOURCELIST resources);
static NTSTATUS SontDeviceControl(PDEVICE_OBJECT device, PIRP irp);
static NTSTATUS SontPnpDispatch(PDEVICE_OBJECT device, PIRP irp);
static VOID SontSignalingWorker(PVOID context);
static NTSTATUS SontStartSignalingWorker(PSONT_DEVICE_EXTENSION ext);
static VOID SontStopSignalingWorker(PSONT_DEVICE_EXTENSION ext);
static NTSTATUS SontReconnectSignalingWorker(PSONT_DEVICE_EXTENSION ext);

static VOID SontInitDefaultProfile(PSONATINA_PROFILE prof)
{
    RtlZeroMemory(prof, sizeof(*prof));
    prof->Size = sizeof(SONATINA_PROFILE);
    prof->Codec = SonatinaCodecSbc;
    prof->SbcSampling = 48000;
    prof->SbcChannelMode = 3;
    prof->SbcBlocks = 16;
    prof->SbcSubbands = 8;
    prof->SbcAllocation = 1;
    prof->SbcMinBitpool = 2;
    prof->SbcMaxBitpool = 53;
    prof->AacSampling = 48000;
    prof->AacChannels = 2;
    prof->AacBitrate = 256;
    prof->LdacSampling = 48000;
    prof->LdacChannelMode = SONT_LDAC_CHMODE_STEREO;
    prof->LdacEqmid = SONT_LDAC_EQMID_HQ;
}

static BOOLEAN SontValidateKernelProfile(const SONATINA_PROFILE *prof)
{
    if (prof == NULL || prof->Size != sizeof(SONATINA_PROFILE)) {
        return FALSE;
    }
    if (SontProfileValidateFields(prof->Codec,
                                  prof->SbcSampling,
                                  prof->SbcChannelMode,
                                  prof->SbcBlocks,
                                  prof->SbcSubbands,
                                  prof->SbcAllocation,
                                  prof->SbcMinBitpool,
                                  prof->SbcMaxBitpool,
                                  prof->AacSampling,
                                  prof->AacChannels,
                                  prof->AacBitrate) == 0) {
        return FALSE;
    }
    if (prof->Codec == SonatinaCodecLdac &&
        SontProfileValidateLdacFields(prof->LdacSampling,
                                      prof->LdacChannelMode,
                                      prof->LdacEqmid) == 0) {
        return FALSE;
    }
    return TRUE;
}

static VOID SontRegReadDword(HANDLE key, PCWSTR name, ULONG *value_out)
{
    UNICODE_STRING val_name;
    UCHAR buf[sizeof(KEY_VALUE_PARTIAL_INFORMATION) + sizeof(ULONG)];
    PKEY_VALUE_PARTIAL_INFORMATION info = (PKEY_VALUE_PARTIAL_INFORMATION)buf;
    ULONG result_len = 0;
    NTSTATUS status;

    RtlInitUnicodeString(&val_name, name);
    status = ZwQueryValueKey(key, &val_name, KeyValuePartialInformation,
                             buf, sizeof(buf), &result_len);
    if (NT_SUCCESS(status) && info->Type == REG_DWORD &&
        info->DataLength == sizeof(ULONG)) {
        RtlCopyMemory(value_out, info->Data, sizeof(ULONG));
    }
}

static VOID SontRegReadCodec(HANDLE key, ULONG *codec_out)
{
    UNICODE_STRING val_name;
    UCHAR buf[sizeof(KEY_VALUE_PARTIAL_INFORMATION) + 32];
    PKEY_VALUE_PARTIAL_INFORMATION info = (PKEY_VALUE_PARTIAL_INFORMATION)buf;
    ULONG result_len = 0;
    NTSTATUS status;

    RtlInitUnicodeString(&val_name, L"Codec");
    status = ZwQueryValueKey(key, &val_name, KeyValuePartialInformation,
                             buf, sizeof(buf), &result_len);
    if (!NT_SUCCESS(status) || info->Type != REG_SZ ||
        info->DataLength < sizeof(WCHAR) || info->DataLength > 32) {
        return;
    }

    {
        UNICODE_STRING found;
        UNICODE_STRING s_pcm;
        UNICODE_STRING s_sbc;
        UNICODE_STRING s_aac;
        UNICODE_STRING s_aptx;
        UNICODE_STRING s_ldac;
        USHORT len = (USHORT)info->DataLength;
        PWCHAR chars = (PWCHAR)info->Data;

        while (len >= sizeof(WCHAR) && chars[(len / sizeof(WCHAR)) - 1] == L'\0') {
            len = (USHORT)(len - sizeof(WCHAR));
        }
        found.Buffer = chars;
        found.Length = len;
        found.MaximumLength = (USHORT)info->DataLength;

        RtlInitUnicodeString(&s_pcm, L"pcm");
        RtlInitUnicodeString(&s_sbc, L"sbc");
        RtlInitUnicodeString(&s_aac, L"aac");
        RtlInitUnicodeString(&s_aptx, L"aptx");
        RtlInitUnicodeString(&s_ldac, L"ldac");

        if (RtlEqualUnicodeString(&found, &s_pcm, TRUE)) {
            *codec_out = SonatinaCodecPcm;
        } else if (RtlEqualUnicodeString(&found, &s_sbc, TRUE)) {
            *codec_out = SonatinaCodecSbc;
        } else if (RtlEqualUnicodeString(&found, &s_aac, TRUE)) {
            *codec_out = SonatinaCodecAac;
        } else if (RtlEqualUnicodeString(&found, &s_aptx, TRUE)) {
            *codec_out = SonatinaCodecAptx;
        } else if (RtlEqualUnicodeString(&found, &s_ldac, TRUE)) {
            *codec_out = SonatinaCodecLdac;
        }
    }
}

static VOID SontLoadProfileFromRegistry(BTH_ADDR remote_addr, PSONATINA_PROFILE prof)
{
    static const WCHAR prefix[] =
        L"\\Registry\\Machine\\SYSTEM\\CurrentControlSet\\Services\\SonatinaAudio\\Parameters\\Devices\\";
    WCHAR path_buf[128];
    char addr_hex[16];
    UNICODE_STRING key_path;
    OBJECT_ATTRIBUTES oa;
    HANDLE key = NULL;
    SONATINA_PROFILE candidate;
    ULONG i;
    ULONG prefix_chars = (ULONG)(sizeof(prefix) / sizeof(prefix[0]) - 1);
    NTSTATUS status;

    if (remote_addr == 0) {
        return;
    }

    SontFormatBdAddrHex((uint64_t)remote_addr, addr_hex);
    for (i = 0; i < prefix_chars; i++) {
        path_buf[i] = prefix[i];
    }
    for (i = 0; i < 12; i++) {
        path_buf[prefix_chars + i] = (WCHAR)addr_hex[i];
    }
    path_buf[prefix_chars + 12] = L'\0';

    RtlInitUnicodeString(&key_path, path_buf);
    InitializeObjectAttributes(&oa, &key_path,
                               OBJ_CASE_INSENSITIVE | OBJ_KERNEL_HANDLE,
                               NULL, NULL);
    status = ZwOpenKey(&key, KEY_READ, &oa);
    if (!NT_SUCCESS(status)) {
        return;
    }

    candidate = *prof;
    SontRegReadCodec(key, &candidate.Codec);
    SontRegReadDword(key, L"SbcSamplingFrequency", &candidate.SbcSampling);
    SontRegReadDword(key, L"SbcChannelMode", &candidate.SbcChannelMode);
    SontRegReadDword(key, L"SbcBlockLength", &candidate.SbcBlocks);
    SontRegReadDword(key, L"SbcSubbands", &candidate.SbcSubbands);
    SontRegReadDword(key, L"SbcAllocationMethod", &candidate.SbcAllocation);
    SontRegReadDword(key, L"SbcMinimumBitpool", &candidate.SbcMinBitpool);
    SontRegReadDword(key, L"SbcMaximumBitpool", &candidate.SbcMaxBitpool);
    SontRegReadDword(key, L"AacSamplingFrequency", &candidate.AacSampling);
    {
        ULONG aac_mode = (candidate.AacChannels == 1) ? 0u : 1u;
        SontRegReadDword(key, L"AacChannelMode", &aac_mode);
        candidate.AacChannels = (aac_mode == 0u) ? 1u : 2u;
    }
    SontRegReadDword(key, L"AacBitrate", &candidate.AacBitrate);
    SontRegReadDword(key, L"LdacSamplingFrequency", &candidate.LdacSampling);
    SontRegReadDword(key, L"LdacChannelMode", &candidate.LdacChannelMode);
    SontRegReadDword(key, L"LdacEqmid", &candidate.LdacEqmid);
    ZwClose(key);

    if (SontValidateKernelProfile(&candidate) &&
        (candidate.Codec == SonatinaCodecPcm ||
         candidate.Codec == SonatinaCodecSbc ||
         candidate.Codec == SonatinaCodecLdac)) {
        *prof = candidate;
        DbgPrint("Sonatina: loaded profile for %s (codec=%lu sbc=%lu Hz bp=%lu..%lu)\n",
                 addr_hex, prof->Codec, prof->SbcSampling,
                 prof->SbcMinBitpool, prof->SbcMaxBitpool);
    } else {
        DbgPrint("Sonatina: ignoring invalid/unsupported registry profile for %s\n",
                 addr_hex);
    }
}

NTSTATUS DriverEntry(PDRIVER_OBJECT driver, PUNICODE_STRING registry_path)
{
    NTSTATUS status;

    status = PcInitializeAdapterDriver(driver, registry_path, SontAddDevice);
    if (!NT_SUCCESS(status)) {
        return status;
    }
    /* Private control interface + PnP teardown; everything else falls through to PortCls. */
    driver->MajorFunction[IRP_MJ_DEVICE_CONTROL] = SontDeviceControl;
    driver->MajorFunction[IRP_MJ_PNP] = SontPnpDispatch;
    return STATUS_SUCCESS;
}

static NTSTATUS SontAddDevice(PDRIVER_OBJECT driver, PDEVICE_OBJECT pdo)
{
    return PcAddAdapterDevice(driver, pdo, SontStartDevice, 2,
                              PORT_CLASS_DEVICE_EXTENSION_SIZE +
                              sizeof(SONT_DEVICE_EXTENSION));
}

static NTSTATUS SontStartDevice(PDEVICE_OBJECT device, PIRP irp,
                                PRESOURCELIST resources)
{
    PSONT_DEVICE_EXTENSION ext = SONT_GET_EXTENSION(device);
    PDEVICE_OBJECT target;
    NTSTATUS status;

    RtlZeroMemory(ext, sizeof(*ext));
    KeInitializeSpinLock(&ext->Lock);
    (void)KeQueryPerformanceCounter(&ext->QpcFreq);
    SontTraceRingInit(&ext->TraceRing);
    KeInitializeEvent(&ext->StopEvent, NotificationEvent, FALSE);
    KeInitializeMutex(&ext->WorkerMutex, 0);
    KeInitializeSpinLock(&ext->PcmSource.Lock);
    KeInitializeEvent(&ext->PcmSource.DataEvent, NotificationEvent, FALSE);
    ext->PcmSource.BackingBuffer = (UCHAR *)ExAllocatePool2(
        POOL_FLAG_NON_PAGED, SONT_PCM_STAGE_BYTES, SONT_POOL_TAG);
    if (ext->PcmSource.BackingBuffer != NULL) {
        SontPcmRingInit(&ext->PcmSource.Ring,
                        ext->PcmSource.BackingBuffer,
                        SONT_PCM_STAGE_BYTES);
    }
    status = PcGetPhysicalDeviceObject(device, &ext->PhysicalDeviceObject);
    if (!NT_SUCCESS(status)) {
        DbgPrint("Sonatina: PcGetPhysicalDeviceObject failed 0x%08X\n", status);
        return status;
    }
    ext->LowerDeviceObject = IoGetLowerDeviceObject(device);
    SontInitDefaultProfile(&ext->Profile);
    ext->State.Size = sizeof(SONATINA_STATE);

    status = IoRegisterDeviceInterface(ext->PhysicalDeviceObject,
                                       &GUID_DEVINTERFACE_SONATINA_CONTROL,
                                       NULL, &ext->ControlInterfaceName);
    if (NT_SUCCESS(status)) {
        ext->ControlInterfaceRegistered = TRUE;
        (void)IoSetDeviceInterfaceState(&ext->ControlInterfaceName, TRUE);
    }

    target = (ext->LowerDeviceObject != NULL) ? ext->LowerDeviceObject
                                              : ext->PhysicalDeviceObject;
    status = SontTransportInitialize(&ext->Transport, target);
    ext->TransportReady = NT_SUCCESS(status);
    if (ext->TransportReady) {
        SontLoadProfileFromRegistry(ext->Transport.RemoteAddress, &ext->Profile);
    } else {
        DbgPrint("Sonatina: transport not ready (0x%08X); control interface only\n",
                 status);
    }

    status = SontRegisterAudioSubdevices(device, irp, resources, &ext->PcmSource);
    if (!NT_SUCCESS(status)) {
        DbgPrint("Sonatina: SontRegisterAudioSubdevices failed 0x%08X\n", status);
        if (ext->PcmSource.BackingBuffer != NULL) {
            ExFreePoolWithTag(ext->PcmSource.BackingBuffer, SONT_POOL_TAG);
            ext->PcmSource.BackingBuffer = NULL;
            SontPcmRingInit(&ext->PcmSource.Ring, NULL, 0);
        }
        if (ext->ControlInterfaceRegistered) {
            (void)IoSetDeviceInterfaceState(&ext->ControlInterfaceName, FALSE);
            RtlFreeUnicodeString(&ext->ControlInterfaceName);
            ext->ControlInterfaceRegistered = FALSE;
        }
        if (ext->LowerDeviceObject != NULL) {
            ObDereferenceObject(ext->LowerDeviceObject);
            ext->LowerDeviceObject = NULL;
        }
        return status;
    }

    if (ext->TransportReady) {
        status = SontStartSignalingWorker(ext);
        if (!NT_SUCCESS(status)) {
            DbgPrint("Sonatina: D3 signaling worker start failed 0x%08X\n", status);
        }
    }

    /* PortCls completes IRP_MN_START_DEVICE after StartDevice returns. */
    return STATUS_SUCCESS;
}

static NTSTATUS SontDeviceControl(PDEVICE_OBJECT device, PIRP irp)
{
    PSONT_DEVICE_EXTENSION ext = SONT_GET_EXTENSION(device);
    PIO_STACK_LOCATION stack = IoGetCurrentIrpStackLocation(irp);
    ULONG code = stack->Parameters.DeviceIoControl.IoControlCode;
    ULONG in_len = stack->Parameters.DeviceIoControl.InputBufferLength;
    ULONG out_len = stack->Parameters.DeviceIoControl.OutputBufferLength;
    PVOID buffer = irp->AssociatedIrp.SystemBuffer;
    KIRQL old_irql;
    NTSTATUS status = STATUS_SUCCESS;

    switch (code) {
    case IOCTL_SONATINA_GET_STATE:
        if (buffer == NULL || out_len < sizeof(SONATINA_STATE)) {
            status = STATUS_BUFFER_TOO_SMALL;
            break;
        }
        KeAcquireSpinLock(&ext->Lock, &old_irql);
        ext->State.Size = sizeof(SONATINA_STATE);
        RtlCopyMemory(buffer, &ext->State, sizeof(SONATINA_STATE));
        KeReleaseSpinLock(&ext->Lock, old_irql);
        irp->IoStatus.Information = sizeof(SONATINA_STATE);
        break;

    case IOCTL_SONATINA_GET_PROFILE:
        if (buffer == NULL || out_len < sizeof(SONATINA_PROFILE)) {
            status = STATUS_BUFFER_TOO_SMALL;
            break;
        }
        KeAcquireSpinLock(&ext->Lock, &old_irql);
        ext->Profile.Size = sizeof(SONATINA_PROFILE);
        RtlCopyMemory(buffer, &ext->Profile, sizeof(SONATINA_PROFILE));
        KeReleaseSpinLock(&ext->Lock, old_irql);
        irp->IoStatus.Information = sizeof(SONATINA_PROFILE);
        break;

    case IOCTL_SONATINA_SET_PROFILE:
        if (buffer == NULL || in_len < sizeof(SONATINA_PROFILE)) {
            status = STATUS_BUFFER_TOO_SMALL;
            break;
        }
        {
            SONATINA_PROFILE candidate;
            RtlCopyMemory(&candidate, buffer, sizeof(SONATINA_PROFILE));
            if (!SontValidateKernelProfile(&candidate)) {
                status = STATUS_INVALID_PARAMETER;
                break;
            }
            if (candidate.Codec != SonatinaCodecPcm &&
                candidate.Codec != SonatinaCodecSbc &&
                candidate.Codec != SonatinaCodecLdac) {
                status = STATUS_NOT_SUPPORTED;
                break;
            }
            KeAcquireSpinLock(&ext->Lock, &old_irql);
            ext->Profile = candidate;
            KeReleaseSpinLock(&ext->Lock, old_irql);
        }
        irp->IoStatus.Information = 0;
        break;

    case IOCTL_SONATINA_GET_STATS:
        if (buffer == NULL || out_len < sizeof(SONATINA_STATE)) {
            status = STATUS_BUFFER_TOO_SMALL;
            break;
        }
        if (out_len >= sizeof(SONATINA_STATS)) {
            PSONATINA_STATS stats_out = (PSONATINA_STATS)buffer;
            SONT_TRACE_ENTRY entries[SONATINA_TRACE_CAPACITY];
            uint32_t copied;
            uint32_t i;

            RtlZeroMemory(stats_out, sizeof(*stats_out));
            KeAcquireSpinLock(&ext->Lock, &old_irql);
            ext->State.Size = sizeof(SONATINA_STATE);
            stats_out->Size = sizeof(SONATINA_STATS);
            stats_out->State = ext->State;
            stats_out->TotalEvents = ext->TraceRing.total_events;
            copied = SontTraceRingSnapshot(&ext->TraceRing, entries, SONATINA_TRACE_CAPACITY);
            KeReleaseSpinLock(&ext->Lock, old_irql);

            stats_out->EventCount = copied;
            for (i = 0; i < copied; i++) {
                stats_out->Events[i].TimestampUs = entries[i].timestamp_us;
                stats_out->Events[i].Kind = entries[i].kind;
                stats_out->Events[i].Arg0 = entries[i].arg0;
                stats_out->Events[i].Arg1 = entries[i].arg1;
            }
            irp->IoStatus.Information = sizeof(SONATINA_STATS);
        } else {
            KeAcquireSpinLock(&ext->Lock, &old_irql);
            ext->State.Size = sizeof(SONATINA_STATE);
            RtlCopyMemory(buffer, &ext->State, sizeof(SONATINA_STATE));
            KeReleaseSpinLock(&ext->Lock, old_irql);
            irp->IoStatus.Information = sizeof(SONATINA_STATE);
        }
        break;

    case IOCTL_SONATINA_RECONNECT:
        if (KeGetCurrentIrql() != PASSIVE_LEVEL) {
            status = STATUS_INVALID_DEVICE_STATE;
            break;
        }
        if (!ext->TransportReady) {
            status = STATUS_DEVICE_NOT_READY;
            break;
        }
        status = SontReconnectSignalingWorker(ext);
        irp->IoStatus.Information = 0;
        break;

    case IOCTL_SONATINA_GET_ADDRESS:
        if (buffer == NULL || out_len < sizeof(SONATINA_DEVICE_INFO)) {
            status = STATUS_BUFFER_TOO_SMALL;
            break;
        }
        {
            SONATINA_DEVICE_INFO dev_info;
            ULONGLONG addr = ext->TransportReady ? (ULONGLONG)ext->Transport.RemoteAddress : 0ULL;
            RtlZeroMemory(&dev_info, sizeof(dev_info));
            dev_info.Size = sizeof(SONATINA_DEVICE_INFO);
            dev_info.RemoteAddress = addr;
            SontFormatBdAddrHex((uint64_t)addr, dev_info.AddressHex);
            RtlCopyMemory(buffer, &dev_info, sizeof(SONATINA_DEVICE_INFO));
        }
        irp->IoStatus.Information = sizeof(SONATINA_DEVICE_INFO);
        break;

    default:
        return PcDispatchIrp(device, irp);
    }

    irp->IoStatus.Status = status;
    IoCompleteRequest(irp, IO_NO_INCREMENT);
    return status;
}

static ULONGLONG SontQueryTimestampUs(PSONT_DEVICE_EXTENSION ext)
{
    LARGE_INTEGER now = KeQueryPerformanceCounter(NULL);
    ULONGLONG freq = (ext->QpcFreq.QuadPart > 0) ? (ULONGLONG)ext->QpcFreq.QuadPart : 10000000ULL;
    ULONGLONG q = (now.QuadPart > 0) ? (ULONGLONG)now.QuadPart : 0ULL;
    return (q / freq) * 1000000ULL + ((q % freq) * 1000000ULL) / freq;
}

static VOID SontTraceLogLocked(PSONT_DEVICE_EXTENSION ext,
                               SONATINA_EVENT_KIND kind,
                               ULONG arg0,
                               ULONG arg1)
{
    SontTraceRingPush(&ext->TraceRing,
                      (uint64_t)SontQueryTimestampUs(ext),
                      (uint32_t)kind,
                      (uint32_t)arg0,
                      (uint32_t)arg1);
}

static VOID SontTraceLog(PSONT_DEVICE_EXTENSION ext,
                         SONATINA_EVENT_KIND kind,
                         ULONG arg0,
                         ULONG arg1)
{
    KIRQL old_irql;
    KeAcquireSpinLock(&ext->Lock, &old_irql);
    SontTraceLogLocked(ext, kind, arg0, arg1);
    KeReleaseSpinLock(&ext->Lock, old_irql);
}

static VOID SontRecordSignalingError(PSONT_DEVICE_EXTENSION ext)
{
    KIRQL old_irql;

    KeAcquireSpinLock(&ext->Lock, &old_irql);
    ext->State.Errors++;
    SontTraceLogLocked(ext, SonatinaEventError, ext->State.Errors, 0);
    KeReleaseSpinLock(&ext->Lock, old_irql);
}

static VOID SontBuildSbcPreference(const SONATINA_PROFILE *prof, SONT_SBC_PARAMS *sbc)
{
    sbc->sampling = (uint32_t)prof->SbcSampling;
    sbc->channel_mode = (uint8_t)prof->SbcChannelMode;
    sbc->blocks = (uint8_t)prof->SbcBlocks;
    sbc->subbands = (uint8_t)prof->SbcSubbands;
    sbc->allocation = (uint8_t)prof->SbcAllocation;
    sbc->min_bitpool = (uint8_t)prof->SbcMinBitpool;
    sbc->max_bitpool = (uint8_t)prof->SbcMaxBitpool;
}

static BOOLEAN SontDequeuePcmFrame(PSONT_PCM_SOURCE src,
                                   ULONG target_rate,
                                   ULONG target_ch,
                                   size_t samples_per_frame,
                                   int16_t *pcm_out,
                                   int16_t *scratch_2x,
                                   BOOLEAN *stream_running)
{
    KIRQL old_irql;
    size_t frame_bytes;
    size_t avail;
    BOOLEAN got = FALSE;

    if (target_ch == 0 || target_ch > SONT_MEDIA_MAX_PCM_CH ||
        samples_per_frame == 0 || samples_per_frame > SONT_MEDIA_MAX_SAMPLES) {
        *stream_running = FALSE;
        return FALSE;
    }
    frame_bytes = samples_per_frame * target_ch * sizeof(int16_t);

    KeAcquireSpinLock(&src->Lock, &old_irql);
    *stream_running = src->StreamRunning;
    avail = SontPcmRingAvailable(&src->Ring);
    if (src->StreamChannels == target_ch && src->StreamSampleRate == target_rate &&
        avail >= frame_bytes) {
        got = (SontPcmRingRead(&src->Ring, (uint8_t *)pcm_out, frame_bytes) == frame_bytes);
    } else if (src->StreamChannels == target_ch &&
               src->StreamSampleRate == target_rate * 2u &&
               avail >= frame_bytes * 2u) {
        if (SontPcmRingRead(&src->Ring, (uint8_t *)scratch_2x, frame_bytes * 2u) ==
            frame_bytes * 2u) {
            size_t i;
            for (i = 0; i < samples_per_frame; i++) {
                size_t c;
                for (c = 0; c < target_ch; c++) {
                    pcm_out[i * target_ch + c] = scratch_2x[(i * 2u) * target_ch + c];
                }
            }
            got = TRUE;
        }
    }
    if (SontPcmRingAvailable(&src->Ring) < frame_bytes) {
        KeClearEvent(&src->DataEvent);
    }
    KeReleaseSpinLock(&src->Lock, old_irql);
    return got;
}

static NTSTATUS SontSendMediaPacket(PSONT_DEVICE_EXTENSION ext,
                                    L2CAP_CHANNEL_HANDLE media_ch,
                                    const SONT_SBC_PARAMS *sbc,
                                    PSONT_SIGNALING_SCRATCH scratch,
                                    size_t rtp_len)
{
    LARGE_INTEGER freq;
    LARGE_INTEGER t0;
    LARGE_INTEGER t1;
    ULONG tx_us = 0;
    uint8_t old_bp;
    uint8_t new_bp = 0;
    int abr_changed;
    NTSTATUS status;
    KIRQL old_irql;

    t0 = KeQueryPerformanceCounter(&freq);
    status = SontTransportSend(&ext->Transport, media_ch, scratch->rtp, (ULONG)rtp_len);
    t1 = KeQueryPerformanceCounter(NULL);

    if (freq.QuadPart > 0 && t1.QuadPart >= t0.QuadPart) {
        tx_us = (ULONG)(((t1.QuadPart - t0.QuadPart) * 1000000LL) / freq.QuadPart);
    }

    old_bp = scratch->abr.current_bitpool;
    abr_changed = SontAbrOnPacketSent(&scratch->abr,
                                      (uint32_t)tx_us,
                                      NT_SUCCESS(status),
                                      &new_bp);
    if (abr_changed) {
        if (!SontMediaSetBitpool(&scratch->media, new_bp)) {
            abr_changed = 0;
        }
    }

    KeAcquireSpinLock(&ext->Lock, &old_irql);
    if (NT_SUCCESS(status)) {
        ext->State.Packets++;
        ext->State.TxTimeUs = tx_us;
    } else {
        ext->State.Errors++;
    }
    ext->State.TxTimeMaxUs = scratch->abr.tx_max_us;
    ext->State.TxTimeAvgUs = scratch->abr.tx_avg_us;
    ext->State.TxTimeHighCount = scratch->abr.tx_high_count;
    ext->State.TxTimeCriticalCount = scratch->abr.tx_critical_count;
    ext->State.AbrAdjustments = scratch->abr.abr_adjustments;
    ext->State.CurrentBitpool = scratch->media.bitpool;
    ext->State.BitrateKbps = (ULONG)SontSbcBitrateKbpsInt(sbc, scratch->media.bitpool);
    ext->State.DelayMs = (scratch->abr.tx_avg_us + 500u) / 1000u;

    if (!NT_SUCCESS(status) || tx_us >= scratch->abr.tx_critical_us) {
        SontTraceLogLocked(ext, SonatinaEventTxCritical, tx_us, (ULONG)status);
    }
    if (abr_changed) {
        SontTraceLogLocked(ext, SonatinaEventAbrStep, (ULONG)old_bp, (ULONG)new_bp);
    }
    KeReleaseSpinLock(&ext->Lock, old_irql);
    return status;
}

static NTSTATUS SontRunSbcMediaLoop(PSONT_DEVICE_EXTENSION ext,
                                    L2CAP_CHANNEL_HANDLE media_ch,
                                    const SONT_SBC_PARAMS *sbc,
                                    PSONT_SIGNALING_SCRATCH scratch)
{
    unsigned samples_per_frame = SontSbcSamplesPerFrame(sbc);
    unsigned channels = SontSbcChannels(sbc);
    PVOID wait_objs[2];
    LARGE_INTEGER wait_due;
    NTSTATUS status = STATUS_SUCCESS;

    if (!SontMediaInit(&scratch->media, sbc, sbc->max_bitpool,
                       SONT_MEDIA_L2CAP_MTU, 0x534F4E54u)) {
        SontRecordSignalingError(ext);
        return STATUS_UNSUCCESSFUL;
    }
    SontAbrInit(&scratch->abr, sbc->min_bitpool, sbc->max_bitpool);
    SontTraceLog(ext, SonatinaEventMediaStart, (ULONG)sbc->sampling, (ULONG)sbc->max_bitpool);

    wait_objs[0] = &ext->PcmSource.DataEvent;
    wait_objs[1] = &ext->StopEvent;
    wait_due.QuadPart = -(LONGLONG)SONT_MEDIA_WAIT_MS * 10000LL;

    while (KeReadStateEvent(&ext->StopEvent) == 0 &&
           ext->Transport.MediaChannel != NULL) {
        BOOLEAN stream_running = FALSE;

        if (SontDequeuePcmFrame(&ext->PcmSource,
                                (ULONG)sbc->sampling,
                                (ULONG)channels,
                                (size_t)samples_per_frame,
                                scratch->pcm_frame,
                                scratch->pcm_2x,
                                &stream_running)) {
            size_t rtp_len = 0;
            int step = SontMediaPushPcmFrame(&scratch->media,
                                             scratch->pcm_frame,
                                             (size_t)samples_per_frame,
                                             scratch->rtp,
                                             sizeof(scratch->rtp),
                                             &rtp_len);
            if (step == 1) {
                status = SontSendMediaPacket(ext, media_ch, sbc, scratch, rtp_len);
                if (!NT_SUCCESS(status)) {
                    break;
                }
            } else if (step < 0) {
                SontRecordSignalingError(ext);
                status = STATUS_UNSUCCESSFUL;
                break;
            }
            continue;
        }

        if (!stream_running && scratch->media.frames_pending > 0) {
            size_t rtp_len = 0;
            if (SontMediaFlush(&scratch->media, scratch->rtp, sizeof(scratch->rtp), &rtp_len) == 1) {
                status = SontSendMediaPacket(ext, media_ch, sbc, scratch, rtp_len);
                if (!NT_SUCCESS(status)) {
                    break;
                }
            }
        }

        if (KeWaitForMultipleObjects(2, wait_objs, WaitAny, Executive,
                                     KernelMode, FALSE, &wait_due, NULL) == STATUS_WAIT_1) {
            status = STATUS_CANCELLED;
            break;
        }
    }

    SontTraceLog(ext, SonatinaEventMediaStop, (ULONG)status, (ULONG)scratch->abr.current_bitpool);
    SontMediaDestroy(&scratch->media);
    return status;
}

static NTSTATUS SontRunLdacMediaLoop(PSONT_DEVICE_EXTENSION ext,
                                      L2CAP_CHANNEL_HANDLE media_ch,
                                      const SONT_LDAC_PARAMS *ldac,
                                      PSONT_SIGNALING_SCRATCH scratch)
{
    SONT_LDAC_ENCODER *encoder;
    unsigned channels = SontLdacChannels(ldac);
    unsigned spf = (ldac->sampling >= 88200u) ? 256u : 128u;
    PVOID wait_objs[2];
    LARGE_INTEGER wait_due;
    uint16_t seq = 0;
    uint32_t ts = 0;
    BOOLEAN sent_first = FALSE;
    uint32_t tx_avg_us = 0;
    uint32_t tx_max_us = 0;
    uint8_t cur_eqmid = ldac->eqmid;
    NTSTATUS status = STATUS_SUCCESS;

    encoder = SontLdacEncoderCreate(ldac, SONT_MEDIA_L2CAP_MTU);
    if (encoder == NULL) {
        SontRecordSignalingError(ext);
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    SontTraceLog(ext, SonatinaEventMediaStart, (ULONG)ldac->sampling, (ULONG)cur_eqmid);

    wait_objs[0] = &ext->PcmSource.DataEvent;
    wait_objs[1] = &ext->StopEvent;
    wait_due.QuadPart = -(LONGLONG)SONT_MEDIA_WAIT_MS * 10000LL;

    while (KeReadStateEvent(&ext->StopEvent) == 0 &&
           ext->Transport.MediaChannel != NULL) {
        BOOLEAN stream_running = FALSE;

        if (SontDequeuePcmFrame(&ext->PcmSource,
                                (ULONG)ldac->sampling,
                                (ULONG)channels,
                                SONT_LDAC_PCM_SAMPLES_PER_PUSH,
                                scratch->pcm_frame,
                                scratch->pcm_2x,
                                &stream_running)) {
            size_t ldac_len = 0;
            uint8_t frame_num = 0;
            if (SontLdacEncode(encoder,
                               scratch->pcm_frame,
                               SONT_LDAC_PCM_SAMPLES_PER_PUSH,
                               scratch->ldac_frames,
                               sizeof(scratch->ldac_frames),
                               &ldac_len,
                               &frame_num) != 0) {
                SontRecordSignalingError(ext);
                status = STATUS_UNSUCCESSFUL;
                break;
            }
            if (ldac_len > 0 && frame_num > 0) {
                SONT_RTP_HEADER hdr;
                size_t payload_len;
                LARGE_INTEGER freq;
                LARGE_INTEGER t0;
                LARGE_INTEGER t1;
                ULONG tx_us = 0;
                KIRQL old_irql;

                SONT_MEMSET(&hdr, 0, sizeof(hdr));
                hdr.version = 2;
                hdr.marker = !sent_first;
                hdr.payload_type = SONT_RTP_PAYLOAD_TYPE;
                hdr.sequence = seq;
                hdr.timestamp = ts;
                hdr.ssrc = 0x534F4E54u;

                if (SontRtpEncodeHeader(&hdr, scratch->rtp) != SONT_RTP_HEADER_LEN) {
                    status = STATUS_UNSUCCESSFUL;
                    break;
                }
                payload_len = SontRtpLdacPayload(scratch->ldac_frames,
                                                 ldac_len,
                                                 frame_num,
                                                 scratch->rtp + SONT_RTP_HEADER_LEN,
                                                 sizeof(scratch->rtp) - SONT_RTP_HEADER_LEN);
                if (payload_len == 0) {
                    status = STATUS_UNSUCCESSFUL;
                    break;
                }

                t0 = KeQueryPerformanceCounter(&freq);
                status = SontTransportSend(&ext->Transport,
                                           media_ch,
                                           scratch->rtp,
                                           (ULONG)(SONT_RTP_HEADER_LEN + payload_len));
                t1 = KeQueryPerformanceCounter(NULL);

                if (freq.QuadPart > 0 && t1.QuadPart >= t0.QuadPart) {
                    tx_us = (ULONG)(((t1.QuadPart - t0.QuadPart) * 1000000LL) / freq.QuadPart);
                }
                if (tx_us > tx_max_us) {
                    tx_max_us = tx_us;
                }
                tx_avg_us = (tx_avg_us == 0u) ? tx_us : ((tx_avg_us * 7u + tx_us) / 8u);

                seq = (uint16_t)(seq + 1u);
                ts += (uint32_t)(((uint64_t)frame_num * (uint64_t)spf * (uint64_t)SONT_RTP_CLOCK) /
                                 (uint64_t)ldac->sampling);
                sent_first = TRUE;

                if ((!NT_SUCCESS(status) || tx_us >= SONT_ABR_DEFAULT_CRITICAL_US) &&
                    cur_eqmid < SONT_LDAC_EQMID_MQ) {
                    uint8_t next_eqmid = (uint8_t)(cur_eqmid + 1u);
                    if (SontLdacSetEqmid(encoder, next_eqmid) == 0) {
                        SontTraceLog(ext, SonatinaEventAbrStep, cur_eqmid, next_eqmid);
                        cur_eqmid = next_eqmid;
                    }
                }

                KeAcquireSpinLock(&ext->Lock, &old_irql);
                if (NT_SUCCESS(status)) {
                    ext->State.Packets++;
                    ext->State.TxTimeUs = tx_us;
                } else {
                    ext->State.Errors++;
                }
                ext->State.TxTimeMaxUs = tx_max_us;
                ext->State.TxTimeAvgUs = tx_avg_us;
                ext->State.DelayMs = (tx_avg_us + 500u) / 1000u;
                ext->State.BitrateKbps = (ULONG)SontLdacBitrateKbpsInt(ldac->sampling, cur_eqmid);
                KeReleaseSpinLock(&ext->Lock, old_irql);

                if (!NT_SUCCESS(status)) {
                    break;
                }
            }
            continue;
        }

        if (KeWaitForMultipleObjects(2, wait_objs, WaitAny, Executive,
                                     KernelMode, FALSE, &wait_due, NULL) == STATUS_WAIT_1) {
            status = STATUS_CANCELLED;
            break;
        }
    }

    SontTraceLog(ext, SonatinaEventMediaStop, (ULONG)status, (ULONG)cur_eqmid);
    SontLdacEncoderDestroy(encoder);
    return status;
}

static VOID SontSignalingWorker(PVOID context)
{
    PSONT_DEVICE_EXTENSION ext = (PSONT_DEVICE_EXTENSION)context;
    PSONT_SIGNALING_SCRATCH scratch = NULL;
    SONATINA_PROFILE prof;
    SONT_SBC_PARAMS pref_sbc;
    L2CAP_CHANNEL_HANDLE sig_ch = NULL;
    L2CAP_CHANNEL_HANDLE media_ch = NULL;
    size_t tx_len = 0;
    ULONG step_count;
    SONT_AVDTP_STEP_RESULT step;
    KIRQL old_irql;
    NTSTATUS status = STATUS_SUCCESS;

    scratch = (PSONT_SIGNALING_SCRATCH)ExAllocatePool2(
        POOL_FLAG_NON_PAGED, sizeof(*scratch), SONT_POOL_TAG);
    if (scratch == NULL) {
        SontRecordSignalingError(ext);
        PsTerminateSystemThread(STATUS_INSUFFICIENT_RESOURCES);
        return;
    }

    status = SontTransportOpen(&ext->Transport, SONT_L2CAP_PSM_AVDTP, &sig_ch);
    if (!NT_SUCCESS(status)) {
        SontRecordSignalingError(ext);
        goto cleanup;
    }
    ext->Transport.SignalingChannel = sig_ch;

    KeAcquireSpinLock(&ext->Lock, &old_irql);
    prof = ext->Profile;
    KeReleaseSpinLock(&ext->Lock, old_irql);
    SontBuildSbcPreference(&prof, &pref_sbc);

    {
        uint8_t pref_codec = SONT_AVDTP_CODEC_SBC;
        if (prof.Codec == SonatinaCodecPcm) {
            pref_codec = SONT_AVDTP_CODEC_RAW;
        } else if (prof.Codec == SonatinaCodecLdac) {
            pref_codec = SONT_AVDTP_CODEC_LDAC;
        }
        SontAvdtpSessionInit(&scratch->session, 1, pref_codec, &pref_sbc);
        if (pref_codec == SONT_AVDTP_CODEC_LDAC) {
            SONT_LDAC_PARAMS pref_ldac;
            pref_ldac.sampling = prof.LdacSampling;
            pref_ldac.channel_mode = (uint8_t)prof.LdacChannelMode;
            pref_ldac.eqmid = (uint8_t)prof.LdacEqmid;
            SontAvdtpSessionSetLdacPreference(&scratch->session, &pref_ldac);
        }
    }

    step = SontAvdtpSessionStartHandshake(&scratch->session,
                                         scratch->tx,
                                         sizeof(scratch->tx),
                                         &tx_len);
    for (step_count = 0;
         step == SONT_AVDTP_STEP_SEND && step_count < SONT_AVDTP_MAX_STEPS;
         step_count++) {
        ULONG rx_tries;

        if (KeReadStateEvent(&ext->StopEvent) != 0) {
            status = STATUS_CANCELLED;
            break;
        }

        status = SontTransportSend(&ext->Transport, sig_ch,
                                   scratch->tx, (ULONG)tx_len);
        if (!NT_SUCCESS(status)) {
            SontRecordSignalingError(ext);
            break;
        }

        step = SONT_AVDTP_STEP_IGNORE;
        for (rx_tries = 0; rx_tries < SONT_AVDTP_MAX_IGNORED; rx_tries++) {
            ULONG rx_len = 0;

            status = SontTransportReceive(&ext->Transport, sig_ch,
                                          scratch->rx, sizeof(scratch->rx),
                                          &rx_len, &ext->StopEvent,
                                          SONT_AVDTP_RX_TIMEOUT_MS);
            if (!NT_SUCCESS(status)) {
                SontRecordSignalingError(ext);
                break;
            }

            step = SontAvdtpSessionOnRx(&scratch->session,
                                        scratch->rx, (size_t)rx_len,
                                        scratch->tx, sizeof(scratch->tx),
                                        &tx_len);
            if (step != SONT_AVDTP_STEP_IGNORE) {
                break;
            }
        }
        if (!NT_SUCCESS(status) || step == SONT_AVDTP_STEP_IGNORE) {
            if (step == SONT_AVDTP_STEP_IGNORE) {
                status = STATUS_IO_TIMEOUT;
                SontRecordSignalingError(ext);
            }
            break;
        }
    }

    if (step == SONT_AVDTP_STEP_ERROR) {
        DbgPrint("Sonatina: D3 AVDTP handshake rejected (err=0x%02X)\n",
                 scratch->session.last_error);
        SontRecordSignalingError(ext);
        status = STATUS_UNSUCCESSFUL;
        goto cleanup;
    }

    if (scratch->session.state == SONT_AVDTP_STATE_STREAMING &&
        KeReadStateEvent(&ext->StopEvent) == 0) {
        status = SontTransportOpenWithFlushTo(&ext->Transport,
                                              SONT_L2CAP_PSM_MEDIA,
                                              SONT_MEDIA_FLUSH_TO_MS,
                                              &media_ch);
        if (!NT_SUCCESS(status)) {
            SontRecordSignalingError(ext);
            goto cleanup;
        }
        ext->Transport.MediaChannel = media_ch;

        KeAcquireSpinLock(&ext->Lock, &old_irql);
        ext->State.Opened = TRUE;
        ext->State.TxFlushTO = SONT_MEDIA_FLUSH_TO_MS;
        if (scratch->session.negotiated.codec == SONT_AVDTP_CODEC_RAW) {
            ext->State.Codec = SonatinaCodecPcm;
            ext->State.NegotiatedSampling = 48000;
            ext->State.NegotiatedChannels = 2;
            ext->State.BitrateKbps = 1536;
            ext->State.CurrentBitpool = 0;
        } else if (scratch->session.negotiated.codec == SONT_AVDTP_CODEC_LDAC) {
            ext->State.Codec = SonatinaCodecLdac;
            ext->State.NegotiatedSampling = scratch->session.negotiated.ldac.sampling;
            ext->State.NegotiatedChannels = (ULONG)SontLdacChannels(&scratch->session.negotiated.ldac);
            ext->State.CurrentBitpool = 0;
            ext->State.BitrateKbps = (ULONG)SontLdacBitrateKbpsInt(
                scratch->session.negotiated.ldac.sampling,
                scratch->session.negotiated.ldac.eqmid);
        } else {
            ext->State.Codec = SonatinaCodecSbc;
            ext->State.NegotiatedSampling = scratch->session.negotiated.sbc.sampling;
            ext->State.NegotiatedChannels = (ULONG)SontSbcChannels(&scratch->session.negotiated.sbc);
            ext->State.CurrentBitpool = scratch->session.negotiated.sbc.max_bitpool;
            ext->State.BitrateKbps = (ULONG)SontSbcBitrateKbpsInt(
                &scratch->session.negotiated.sbc,
                scratch->session.negotiated.sbc.max_bitpool);
        }
        SontTraceLogLocked(ext,
                           SonatinaEventAvdtpState,
                           (ULONG)scratch->session.state,
                           (ULONG)scratch->session.selected_acp_seid);
        KeReleaseSpinLock(&ext->Lock, old_irql);

        DbgPrint("Sonatina: D3 AVDTP streaming open (SEID=%u, %lu Hz, %lu kbps)\n",
                 (unsigned)scratch->session.selected_acp_seid,
                 ext->State.NegotiatedSampling,
                 ext->State.BitrateKbps);

        if (scratch->session.negotiated.codec == SONT_AVDTP_CODEC_SBC) {
            status = SontRunSbcMediaLoop(ext, media_ch,
                                         &scratch->session.negotiated.sbc,
                                         scratch);
        } else if (scratch->session.negotiated.codec == SONT_AVDTP_CODEC_LDAC) {
            status = SontRunLdacMediaLoop(ext, media_ch,
                                          &scratch->session.negotiated.ldac,
                                          scratch);
        }
    }

cleanup:
    KeAcquireSpinLock(&ext->Lock, &old_irql);
    ext->State.Opened = FALSE;
    KeReleaseSpinLock(&ext->Lock, old_irql);

    if (scratch != NULL) {
        if (ext->ReconnectRequested &&
            scratch->session.state == SONT_AVDTP_STATE_STREAMING &&
            ext->Transport.SignalingChannel != NULL) {
            size_t close_len = 0;
            if (SontAvdtpSessionClose(&scratch->session, scratch->tx,
                                      sizeof(scratch->tx), &close_len) == SONT_AVDTP_STEP_SEND) {
                (void)SontTransportSend(&ext->Transport, ext->Transport.SignalingChannel,
                                        scratch->tx, (ULONG)close_len);
            }
        }
        ExFreePoolWithTag(scratch, SONT_POOL_TAG);
    }
    SontTransportClose(&ext->Transport, &ext->Transport.MediaChannel);
    SontTransportClose(&ext->Transport, &ext->Transport.SignalingChannel);
    PsTerminateSystemThread(status);
}

static NTSTATUS SontStartSignalingWorkerLocked(PSONT_DEVICE_EXTENSION ext)
{
    HANDLE thread_handle = NULL;
    NTSTATUS status;

    if (ext->WorkerThread != NULL) {
        return STATUS_SUCCESS;
    }
    KeClearEvent(&ext->StopEvent);
    status = PsCreateSystemThread(&thread_handle, THREAD_ALL_ACCESS,
                                  NULL, NULL, NULL,
                                  SontSignalingWorker, ext);
    if (!NT_SUCCESS(status)) {
        return status;
    }

    status = ObReferenceObjectByHandle(thread_handle, SYNCHRONIZE,
                                       *PsThreadType, KernelMode,
                                       &ext->WorkerThread, NULL);
    (void)ZwClose(thread_handle);
    if (!NT_SUCCESS(status)) {
        ext->WorkerThread = NULL;
        KeSetEvent(&ext->StopEvent, IO_NO_INCREMENT, FALSE);
    }
    return status;
}

static VOID SontStopSignalingWorkerLocked(PSONT_DEVICE_EXTENSION ext)
{
    KeSetEvent(&ext->StopEvent, IO_NO_INCREMENT, FALSE);
    if (ext->WorkerThread != NULL) {
        KeWaitForSingleObject(ext->WorkerThread, Executive, KernelMode, FALSE, NULL);
        ObDereferenceObject(ext->WorkerThread);
        ext->WorkerThread = NULL;
    }
}

static NTSTATUS SontStartSignalingWorker(PSONT_DEVICE_EXTENSION ext)
{
    NTSTATUS status;

    KeWaitForSingleObject(&ext->WorkerMutex, Executive, KernelMode, FALSE, NULL);
    if (InterlockedCompareExchange(&ext->Removing, 0, 0) != 0) {
        KeReleaseMutex(&ext->WorkerMutex, FALSE);
        return STATUS_DELETE_PENDING;
    }
    status = SontStartSignalingWorkerLocked(ext);
    KeReleaseMutex(&ext->WorkerMutex, FALSE);
    return status;
}

static VOID SontStopSignalingWorker(PSONT_DEVICE_EXTENSION ext)
{
    KeWaitForSingleObject(&ext->WorkerMutex, Executive, KernelMode, FALSE, NULL);
    SontStopSignalingWorkerLocked(ext);
    KeReleaseMutex(&ext->WorkerMutex, FALSE);
}

static NTSTATUS SontReconnectSignalingWorker(PSONT_DEVICE_EXTENSION ext)
{
    KIRQL old_irql;
    NTSTATUS status;

    KeWaitForSingleObject(&ext->WorkerMutex, Executive, KernelMode, FALSE, NULL);
    if (InterlockedCompareExchange(&ext->Removing, 0, 0) != 0) {
        KeReleaseMutex(&ext->WorkerMutex, FALSE);
        return STATUS_DELETE_PENDING;
    }
    ext->ReconnectRequested = TRUE;
    SontStopSignalingWorkerLocked(ext);
    ext->ReconnectRequested = FALSE;

    KeAcquireSpinLock(&ext->Lock, &old_irql);
    ext->State.Opened = FALSE;
    SontTraceLogLocked(ext, SonatinaEventReconnect, ext->Profile.Codec, ext->Profile.SbcMaxBitpool);
    KeReleaseSpinLock(&ext->Lock, old_irql);

    status = SontStartSignalingWorkerLocked(ext);
    KeReleaseMutex(&ext->WorkerMutex, FALSE);
    return status;
}

static NTSTATUS SontPnpDispatch(PDEVICE_OBJECT device, PIRP irp)
{
    PIO_STACK_LOCATION stack = IoGetCurrentIrpStackLocation(irp);
    PSONT_DEVICE_EXTENSION ext = SONT_GET_EXTENSION(device);

    if (stack->MinorFunction == IRP_MN_SURPRISE_REMOVAL ||
        stack->MinorFunction == IRP_MN_REMOVE_DEVICE) {
        InterlockedExchange(&ext->Removing, 1);
        if (ext->ControlInterfaceRegistered) {
            (void)IoSetDeviceInterfaceState(&ext->ControlInterfaceName, FALSE);
        }
        SontStopSignalingWorker(ext);
    }

    if (stack->MinorFunction == IRP_MN_REMOVE_DEVICE) {
        SontTransportTeardown(&ext->Transport);
        ext->TransportReady = FALSE;
        if (ext->PcmSource.BackingBuffer != NULL) {
            ExFreePoolWithTag(ext->PcmSource.BackingBuffer, SONT_POOL_TAG);
            ext->PcmSource.BackingBuffer = NULL;
            SontPcmRingInit(&ext->PcmSource.Ring, NULL, 0);
        }
        if (ext->ControlInterfaceRegistered) {
            RtlFreeUnicodeString(&ext->ControlInterfaceName);
            ext->ControlInterfaceRegistered = FALSE;
        }
        if (ext->LowerDeviceObject != NULL) {
            ObDereferenceObject(ext->LowerDeviceObject);
            ext->LowerDeviceObject = NULL;
        }
    }

    return PcDispatchIrp(device, irp);
}
