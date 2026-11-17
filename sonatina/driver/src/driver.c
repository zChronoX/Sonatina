/* SonatinaAudio.sys - WDM/PortCls function driver for the Bluetooth AudioSink
 * devnode (A2DP source).
 *
 *  - PortCls WaveRT + Topology subdevices expose the render endpoint
 *    (miniport_*.cpp); the WaveRT timer copies PCM into SONT_PCM_SOURCE.
 *  - One system thread per device (SontSignalingWorker) supervises the
 *    Bluetooth connection: it accepts the headset's own reconnection or
 *    connects out, runs AVDTP in both roles on PSM 0x0019 (avdtp_session.c),
 *    and paces SBC/LDAC encoding on a media clock, streaming silence when
 *    Windows is idle so the A2DP stream is never suspended by us.
 *  - A private device interface (include/public.h) serves the service/GUI.
 * See DESIGN.md.
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

/* Signalling / connection policy */
#define SONT_CMD_TIMEOUT_MS          5000u   /* AVDTP response timeout */
#define SONT_CONNECT_TIMEOUT_MS      15000u  /* outgoing L2CAP (includes paging) */
#define SONT_REMOTE_FIRST_MS         1000u   /* let the headset configure after it connects */
#define SONT_REHANDSHAKE_MS          1500u   /* after a remote Close/Abort */
#define SONT_MEDIA_OPEN_WAIT_MS      3000u   /* remote Open without its transport channel */
#define SONT_RESUME_MIN_MS           3000u   /* after a remote Suspend */
#define SONT_RESUME_MAX_MS           60000u
#define SONT_RECONNECT_AFTER_DROP_MS 1000u
#define SONT_EAGER_ATTEMPTS          4u      /* outgoing attempts before waiting for audio */
#define SONT_IDLE_POLL_MS            1000u

/* Media path */
#define SONT_MEDIA_FLUSH_TO_MS       0u      /* 0 = no L2CAP flush timeout: never discard
                                                * audio the controller is still sending */
#define SONT_MEDIA_LEAD_US           20000u  /* send-ahead */
#define SONT_MEDIA_PREFILL_MS        30u     /* jitter buffer on the WaveRT tap */
#define SONT_MEDIA_MAX_BUFFER_MS     150u
#define SONT_MEDIA_MAX_BURST         24u
#define SONT_MEDIA_MAX_WAIT_MS       10u
#define SONT_MEDIA_MAX_TX_FAILURES   16u     /* consecutive failed completions = link gone */
/* ABR metric: audio already queued to the controller when a packet is submitted */
#define SONT_ABR_QUEUE_LOW_US        50000u
#define SONT_ABR_QUEUE_HIGH_US       90000u
#define SONT_ABR_QUEUE_CRITICAL_US   130000u
#define SONT_SBC_ABR_FLOOR           20u     /* never below this bitpool (2 is unlistenable) */
#define SONT_LDAC_RECOVER_PACKETS    1500u   /* clean packets before raising LDAC quality */
#define SONT_TRACE_CRITICAL_GAP_US   250000u /* rate limit for TxCritical trace events */
#define SONT_MEDIA_MAX_PCM_CH        2u
#define SONT_MEDIA_MAX_SAMPLES       128u
#define SONT_TIMER_RESOLUTION_100NS  20000u  /* 2 ms while streaming */
#define SONT_SIGNAL_SCAN_BYTES       4096u   /* ~20 ms of 48 kHz stereo */
#define SONT_SIGNAL_THRESHOLD        16

typedef struct _SONT_MEDIA_ENGINE {
    BOOLEAN Active;
    BOOLEAN TimerResolutionSet;
    BOOLEAN SentFirst;
    UCHAR Codec;                 /* SONT_AVDTP_CODEC_SBC / _LDAC */
    ULONG Rate;
    ULONG Channels;
    ULONG SamplesPerPush;        /* PCM frames per encoder call */
    ULONG FrameSamples;          /* LDAC samples per codec frame (RTP timestamp) */
    USHORT Mtu;
    SONT_MEDIA_CLOCK Clock;
    SONT_PCM_PULL Pull;
    ULONG PullInRate;
    ULONG PullInChannels;
    SONT_LDAC_ENCODER *Ldac;
    uint8_t LdacEqmid;
    uint16_t Seq;
    uint32_t Timestamp;
    uint8_t LdacMaxEqmid;        /* best quality negotiated (lowest EQMID) */
    ULONG LdacCleanPackets;
    ULONG TxAvgUs;               /* completion latency */
    ULONG TxMaxUs;
    ULONG TxFailures;
    ULONG Dropped;               /* packets dropped because the queue was full */
    ULONGLONG LastCriticalTraceUs;
} SONT_MEDIA_ENGINE, *PSONT_MEDIA_ENGINE;

typedef struct _SONT_SIGNALING_SCRATCH {
    SONT_AVDTP_SESSION session;
    SONT_MEDIA_PACKETIZER media;   /* SBC packetizer */
    SONT_ABR abr;
    SONT_MEDIA_ENGINE engine;
    SONT_ASYNC_RX rx;              /* outstanding receive on the signalling channel */
    SONT_TX_QUEUE txq;             /* media packets in flight */
    KWAIT_BLOCK wait_blocks[4];
    BOOLEAN StartRejected;
    uint8_t tx[SONT_AVDTP_MAX_PDU];
    uint8_t rtp[1200];
    uint8_t ldac_frames[1024];
    int16_t pcm_frame[SONT_MEDIA_MAX_SAMPLES * SONT_MEDIA_MAX_PCM_CH];
} SONT_SIGNALING_SCRATCH, *PSONT_SIGNALING_SCRATCH;

static NTSTATUS SontAddDevice(PDRIVER_OBJECT driver, PDEVICE_OBJECT pdo);
static NTSTATUS SontStartDevice(PDEVICE_OBJECT device, PIRP irp, PRESOURCELIST resources);
static NTSTATUS SontDeviceControl(PDEVICE_OBJECT device, PIRP irp);
static NTSTATUS SontCreateCloseDispatch(PDEVICE_OBJECT device, PIRP irp);

/* The control interface is registered with this reference string, so opens of
 * it reach us with FileObject->FileName == "\SonatinaCtl" and are kept apart
 * from the KS/PortCls filter and pin opens on the same device object. */
#define SONT_CONTROL_REF_STRING L"SonatinaCtl"

/* Its address marks our control handles in FileObject->FsContext (PortCls/KS
 * store their object header there, so the two can never be confused). */
static LONG g_SontControlTag;

static BOOLEAN SontIsControlFile(PFILE_OBJECT file)
{
    return (file != NULL && file->FsContext == (PVOID)&g_SontControlTag) ? TRUE : FALSE;
}
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
    driver->MajorFunction[IRP_MJ_CREATE] = SontCreateCloseDispatch;
    driver->MajorFunction[IRP_MJ_CLEANUP] = SontCreateCloseDispatch;
    driver->MajorFunction[IRP_MJ_CLOSE] = SontCreateCloseDispatch;
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

    {
        UNICODE_STRING ref = RTL_CONSTANT_STRING(SONT_CONTROL_REF_STRING);
        status = IoRegisterDeviceInterface(ext->PhysicalDeviceObject,
                                           &GUID_DEVINTERFACE_SONATINA_CONTROL,
                                           &ref, &ext->ControlInterfaceName);
    }
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

static NTSTATUS SontCreateCloseDispatch(PDEVICE_OBJECT device, PIRP irp)
{
    PIO_STACK_LOCATION stack = IoGetCurrentIrpStackLocation(irp);
    PFILE_OBJECT file = stack->FileObject;
    BOOLEAN ours = FALSE;

    if (stack->MajorFunction == IRP_MJ_CREATE) {
        if (file != NULL && file->FileName.Length != 0) {
            UNICODE_STRING want = RTL_CONSTANT_STRING(SONT_CONTROL_REF_STRING);
            UNICODE_STRING got = file->FileName;
            if (got.Length >= sizeof(WCHAR) && got.Buffer[0] == L'\\') {
                got.Buffer++;
                got.Length -= sizeof(WCHAR);
                got.MaximumLength -= sizeof(WCHAR);
            }
            if (RtlEqualUnicodeString(&got, &want, TRUE)) {
                file->FsContext = (PVOID)&g_SontControlTag;
                ours = TRUE;
            }
        }
    } else {
        ours = SontIsControlFile(file);
    }
    if (!ours) {
        return PcDispatchIrp(device, irp);  /* KS filter / pin objects */
    }
    irp->IoStatus.Status = STATUS_SUCCESS;
    irp->IoStatus.Information = 0;
    IoCompleteRequest(irp, IO_NO_INCREMENT);
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

    if (!SontIsControlFile(stack->FileObject)) {
        return PcDispatchIrp(device, irp);  /* IOCTL_KS_* on filter / pin handles */
    }

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
        /* Never hand our own file object to PortCls: its FsContext is not a KS header. */
        status = STATUS_INVALID_DEVICE_REQUEST;
        irp->IoStatus.Information = 0;
        break;
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

/* ------------------------------------------------------------------ media engine
 *
 * Encoding is paced by SONT_MEDIA_CLOCK (QPC based), not by the arrival of PCM:
 * when the audio engine is idle or late the headset receives digital silence,
 * so the A2DP stream is never starved and never needs a Suspend/Start cycle.
 */

static ULONGLONG SontNowMs(PSONT_DEVICE_EXTENSION ext)
{
    return SontQueryTimestampUs(ext) / 1000ULL;
}

static USHORT SontClampMtu(USHORT mtu, size_t cap)
{
    if (mtu < 48u) {
        mtu = 672u;
    }
    if ((size_t)mtu > cap) {
        mtu = (USHORT)cap;
    }
    return mtu;
}

static VOID SontMediaStop(PSONT_DEVICE_EXTENSION ext, PSONT_SIGNALING_SCRATCH scratch)
{
    PSONT_MEDIA_ENGINE e = &scratch->engine;
    KIRQL old_irql;

    if (!e->Active) {
        return;
    }
    SontTxQueueDrain(&scratch->txq);  /* before the media channel can be closed */
    if (e->Codec == SONT_AVDTP_CODEC_SBC) {
        SontMediaDestroy(&scratch->media);
    }
    if (e->Ldac != NULL) {
        SontLdacEncoderDestroy(e->Ldac);
        e->Ldac = NULL;
    }
    if (e->TimerResolutionSet) {
        (void)ExSetTimerResolution(0, FALSE);
    }
    KeAcquireSpinLock(&ext->Lock, &old_irql);
    ext->State.Opened = FALSE;
    SontTraceLogLocked(ext, SonatinaEventMediaStop, (ULONG)e->Codec, e->Pull.underruns);
    KeReleaseSpinLock(&ext->Lock, old_irql);
    RtlZeroMemory(e, sizeof(*e));
}

static NTSTATUS SontMediaStart(PSONT_DEVICE_EXTENSION ext, PSONT_SIGNALING_SCRATCH scratch,
                               USHORT mtu)
{
    PSONT_MEDIA_ENGINE e = &scratch->engine;
    const SONT_CAPABILITIES *cfg = &scratch->session.negotiated;
    KIRQL old_irql;

    SontMediaStop(ext, scratch);
    mtu = SontClampMtu(mtu, sizeof(scratch->rtp));

    if (cfg->codec == SONT_AVDTP_CODEC_SBC) {
        const SONT_SBC_PARAMS *sbc = &cfg->sbc;
        e->Rate = sbc->sampling;
        e->Channels = SontSbcChannels(sbc);
        e->SamplesPerPush = SontSbcSamplesPerFrame(sbc);
        if (e->SamplesPerPush == 0 || e->SamplesPerPush > SONT_MEDIA_MAX_SAMPLES ||
            !SontMediaInit(&scratch->media, sbc, sbc->max_bitpool, mtu, 0x534F4E54u)) {
            RtlZeroMemory(e, sizeof(*e));
            return STATUS_UNSUCCESSFUL;
        }
        {
            uint8_t floor_bp = (sbc->max_bitpool > SONT_SBC_ABR_FLOOR) ? SONT_SBC_ABR_FLOOR
                                                                       : sbc->max_bitpool;
            if (floor_bp < sbc->min_bitpool) {
                floor_bp = sbc->min_bitpool;
            }
            SontAbrInit(&scratch->abr, floor_bp, sbc->max_bitpool);
            SontAbrSetThresholds(&scratch->abr, SONT_ABR_QUEUE_LOW_US, SONT_ABR_QUEUE_HIGH_US,
                                 SONT_ABR_QUEUE_CRITICAL_US);
        }
    } else if (cfg->codec == SONT_AVDTP_CODEC_LDAC) {
        USHORT ldac_mtu = (USHORT)((mtu > SONT_RTP_HEADER_LEN + 1u) ? mtu - SONT_RTP_HEADER_LEN - 1u : 0u);
        if (ldac_mtu < 679u) {
            DbgPrint("Sonatina: headset MTU %u is small for LDAC\n", (unsigned)mtu);
            ldac_mtu = 679u;
        }
        if (ldac_mtu > 1023u) {
            ldac_mtu = 1023u;
        }
        e->Rate = cfg->ldac.sampling;
        e->Channels = SontLdacChannels(&cfg->ldac);
        e->SamplesPerPush = SONT_LDAC_PCM_SAMPLES_PER_PUSH;
        e->FrameSamples = (cfg->ldac.sampling >= 88200u) ? 256u : 128u;
        e->Ldac = SontLdacEncoderCreate(&cfg->ldac, ldac_mtu);
        if (e->Ldac == NULL) {
            RtlZeroMemory(e, sizeof(*e));
            return STATUS_INSUFFICIENT_RESOURCES;
        }
        e->LdacEqmid = cfg->ldac.eqmid;
        e->LdacMaxEqmid = cfg->ldac.eqmid;
    } else {
        return STATUS_NOT_SUPPORTED;
    }
    if (e->Channels == 0 || e->Channels > SONT_MEDIA_MAX_PCM_CH) {
        SontMediaStop(ext, scratch);
        return STATUS_NOT_SUPPORTED;
    }

    e->Codec = cfg->codec;
    e->Mtu = mtu;
    e->PullInRate = 0;  /* forces SontPcmPullInit on the first pull */
    SontMediaClockStart(&e->Clock, SontQueryTimestampUs(ext), e->Rate, SONT_MEDIA_LEAD_US);
    (void)ExSetTimerResolution(SONT_TIMER_RESOLUTION_100NS, TRUE);
    e->TimerResolutionSet = TRUE;
    e->Active = TRUE;

    KeAcquireSpinLock(&ext->Lock, &old_irql);
    ext->State.Opened = TRUE;
    ext->State.TxFlushTO = SONT_MEDIA_FLUSH_TO_MS;
    ext->State.NegotiatedSampling = e->Rate;
    ext->State.NegotiatedChannels = e->Channels;
    if (e->Codec == SONT_AVDTP_CODEC_LDAC) {
        ext->State.Codec = SonatinaCodecLdac;
        ext->State.CurrentBitpool = 0;
        ext->State.BitrateKbps = (ULONG)SontLdacBitrateKbpsInt(e->Rate, e->LdacEqmid);
    } else {
        ext->State.Codec = SonatinaCodecSbc;
        ext->State.CurrentBitpool = cfg->sbc.max_bitpool;
        ext->State.BitrateKbps = (ULONG)SontSbcBitrateKbpsInt(&cfg->sbc, cfg->sbc.max_bitpool);
    }
    SontTraceLogLocked(ext, SonatinaEventMediaStart, e->Rate, (ULONG)mtu);
    KeReleaseSpinLock(&ext->Lock, old_irql);

    DbgPrint("Sonatina: streaming %s %lu Hz, %lu ch, MTU %u\n",
             (e->Codec == SONT_AVDTP_CODEC_LDAC) ? "LDAC" : "SBC",
             e->Rate, e->Channels, (unsigned)mtu);
    return STATUS_SUCCESS;
}

/* One codec push worth of PCM from the WaveRT tap (or silence). */
static VOID SontMediaPullPcm(PSONT_DEVICE_EXTENSION ext, PSONT_SIGNALING_SCRATCH scratch)
{
    PSONT_MEDIA_ENGINE e = &scratch->engine;
    PSONT_PCM_SOURCE src = &ext->PcmSource;
    size_t frames = e->SamplesPerPush;
    KIRQL old_irql;

    KeAcquireSpinLock(&src->Lock, &old_irql);
    if (src->BackingBuffer != NULL && src->StreamRunning &&
        src->StreamSampleRate != 0 && src->StreamChannels != 0) {
        if (src->StreamSampleRate != e->PullInRate || src->StreamChannels != e->PullInChannels) {
            SontPcmPullInit(&e->Pull, src->StreamSampleRate, src->StreamChannels,
                            e->Rate, e->Channels,
                            SONT_MEDIA_PREFILL_MS, SONT_MEDIA_MAX_BUFFER_MS);
            e->PullInRate = src->StreamSampleRate;
            e->PullInChannels = src->StreamChannels;
        }
        (void)SontPcmPull(&e->Pull, &src->Ring, scratch->pcm_frame, frames);
        if (SontPcmRingAvailable(&src->Ring) == 0) {
            KeClearEvent(&src->DataEvent);
        }
    } else {
        RtlZeroMemory(scratch->pcm_frame, frames * e->Channels * sizeof(int16_t));
        e->Pull.playing = false;  /* refill the jitter buffer when audio resumes */
    }
    KeReleaseSpinLock(&src->Lock, old_irql);
}

/* Collects finished transfers: completion latency and failures. Returns an
 * error once the link looks dead (many consecutive failed completions). */
static NTSTATUS SontReapTx(PSONT_DEVICE_EXTENSION ext, PSONT_SIGNALING_SCRATCH scratch)
{
    PSONT_MEDIA_ENGINE e = &scratch->engine;
    NTSTATUS st;
    ULONG lat_us;
    KIRQL old_irql;

    while (SontTxQueueReap(&scratch->txq, &st, &lat_us)) {
        if (lat_us > e->TxMaxUs) {
            e->TxMaxUs = lat_us;
        }
        e->TxAvgUs = (e->TxAvgUs == 0u) ? lat_us : ((e->TxAvgUs * 7u + lat_us) / 8u);
        KeAcquireSpinLock(&ext->Lock, &old_irql);
        if (NT_SUCCESS(st)) {
            ext->State.Packets++;
        } else {
            ext->State.Errors++;
        }
        ext->State.TxTimeUs = lat_us;
        ext->State.TxTimeAvgUs = e->TxAvgUs;
        ext->State.TxTimeMaxUs = e->TxMaxUs;
        KeReleaseSpinLock(&ext->Lock, old_irql);
        if (NT_SUCCESS(st)) {
            e->TxFailures = 0;
        } else if (st != STATUS_CANCELLED && ++e->TxFailures >= SONT_MEDIA_MAX_TX_FAILURES) {
            return st;
        }
    }
    return STATUS_SUCCESS;
}

/* Queues one RTP packet. *queued_us gets the audio already in flight before it
 * (the ABR metric); returns STATUS_DEVICE_BUSY when it had to be dropped. */
static NTSTATUS SontQueueMediaPacket(PSONT_DEVICE_EXTENSION ext, PSONT_SIGNALING_SCRATCH scratch,
                                     const uint8_t *data, size_t len, ULONG audio_us,
                                     ULONG *queued_us)
{
    PSONT_MEDIA_ENGINE e = &scratch->engine;
    PSONT_TX_SLOT slot;
    LONG queued;
    ULONGLONG now_us;

    queued = InterlockedCompareExchange(&scratch->txq.InFlightAudioUs, 0, 0);
    *queued_us = (queued > 0) ? (ULONG)queued : 0u;
    slot = (len <= SONT_TX_SLOT_BYTES) ? SontTxQueueAcquire(&scratch->txq) : NULL;
    if (slot != NULL) {
        RtlCopyMemory(slot->Buffer, data, len);
        SontTxQueueSubmit(slot, ext->Transport.MediaChannel, (ULONG)len, audio_us);
    } else {
        e->Dropped++;
    }

    now_us = SontQueryTimestampUs(ext);
    if ((slot == NULL || *queued_us >= SONT_ABR_QUEUE_CRITICAL_US) &&
        now_us - e->LastCriticalTraceUs >= SONT_TRACE_CRITICAL_GAP_US) {
        e->LastCriticalTraceUs = now_us;
        SontTraceLog(ext, SonatinaEventTxCritical, *queued_us / 1000u, e->Dropped);
    }
    return (slot != NULL) ? STATUS_SUCCESS : STATUS_DEVICE_BUSY;
}

static ULONG SontAudioUs(ULONG samples, ULONG rate)
{
    return (rate != 0) ? (ULONG)(((ULONGLONG)samples * 1000000ULL) / rate) : 0u;
}

static VOID SontSendSbcPacket(PSONT_DEVICE_EXTENSION ext, PSONT_SIGNALING_SCRATCH scratch,
                              size_t rtp_len)
{
    PSONT_MEDIA_ENGINE e = &scratch->engine;
    const SONT_SBC_PARAMS *sbc = &scratch->session.negotiated.sbc;
    ULONG frames = (rtp_len > SONT_RTP_HEADER_LEN) ? (ULONG)(scratch->rtp[SONT_RTP_HEADER_LEN] & 0x0Fu) : 0u;
    ULONG queued_us = 0;
    uint8_t old_bp = scratch->abr.current_bitpool;
    uint8_t new_bp = 0;
    int abr_changed;
    NTSTATUS status;
    KIRQL old_irql;

    status = SontQueueMediaPacket(ext, scratch, scratch->rtp, rtp_len,
                                  SontAudioUs(frames * e->SamplesPerPush, e->Rate), &queued_us);
    abr_changed = SontAbrOnPacketSent(&scratch->abr, queued_us, NT_SUCCESS(status), &new_bp);
    if (abr_changed && !SontMediaSetBitpool(&scratch->media, new_bp)) {
        abr_changed = 0;
    }

    KeAcquireSpinLock(&ext->Lock, &old_irql);
    ext->State.TxTimeHighCount = scratch->abr.tx_high_count;
    ext->State.TxTimeCriticalCount = scratch->abr.tx_critical_count;
    ext->State.AbrAdjustments = scratch->abr.abr_adjustments;
    ext->State.CurrentBitpool = scratch->media.bitpool;
    ext->State.BitrateKbps = (ULONG)SontSbcBitrateKbpsInt(sbc, scratch->media.bitpool);
    ext->State.LostPackets = e->Pull.underruns;
    if (abr_changed) {
        SontTraceLogLocked(ext, SonatinaEventAbrStep, (ULONG)old_bp, (ULONG)new_bp);
    }
    KeReleaseSpinLock(&ext->Lock, old_irql);
}

static NTSTATUS SontPushLdac(PSONT_DEVICE_EXTENSION ext, PSONT_SIGNALING_SCRATCH scratch)
{
    PSONT_MEDIA_ENGINE e = &scratch->engine;
    SONT_RTP_HEADER hdr;
    size_t ldac_len = 0;
    size_t payload_len;
    uint8_t frame_num = 0;
    ULONG queued_us = 0;
    NTSTATUS status;
    KIRQL old_irql;

    if (SontLdacEncode(e->Ldac, scratch->pcm_frame, SONT_LDAC_PCM_SAMPLES_PER_PUSH,
                       scratch->ldac_frames, sizeof(scratch->ldac_frames),
                       &ldac_len, &frame_num) != 0) {
        return STATUS_UNSUCCESSFUL;
    }
    if (ldac_len == 0 || frame_num == 0) {
        return STATUS_SUCCESS;  /* libldac is buffering */
    }

    SONT_MEMSET(&hdr, 0, sizeof(hdr));
    hdr.version = 2;
    hdr.marker = !e->SentFirst;
    hdr.payload_type = SONT_RTP_PAYLOAD_TYPE;
    hdr.sequence = e->Seq;
    hdr.timestamp = e->Timestamp;   /* in samples, like SBC */
    hdr.ssrc = 0x534F4E54u;
    if (SontRtpEncodeHeader(&hdr, scratch->rtp) != SONT_RTP_HEADER_LEN) {
        return STATUS_UNSUCCESSFUL;
    }
    payload_len = SontRtpLdacPayload(scratch->ldac_frames, ldac_len, frame_num,
                                     scratch->rtp + SONT_RTP_HEADER_LEN,
                                     sizeof(scratch->rtp) - SONT_RTP_HEADER_LEN);
    if (payload_len == 0) {
        return STATUS_UNSUCCESSFUL;
    }

    status = SontQueueMediaPacket(ext, scratch, scratch->rtp, SONT_RTP_HEADER_LEN + payload_len,
                                  SontAudioUs((ULONG)frame_num * e->FrameSamples, e->Rate),
                                  &queued_us);
    e->Seq = (uint16_t)(e->Seq + 1u);
    e->Timestamp += (uint32_t)frame_num * e->FrameSamples;
    e->SentFirst = TRUE;

    /* LDAC ABR on the same queue metric: step quality down on a full queue,
     * back up after a long clean run. */
    if (!NT_SUCCESS(status) || queued_us >= SONT_ABR_QUEUE_CRITICAL_US) {
        e->LdacCleanPackets = 0;
        if (e->LdacEqmid < SONT_LDAC_EQMID_MQ &&
            SontLdacSetEqmid(e->Ldac, (uint8_t)(e->LdacEqmid + 1u)) == 0) {
            SontTraceLog(ext, SonatinaEventAbrStep, e->LdacEqmid, (ULONG)e->LdacEqmid + 1u);
            e->LdacEqmid++;
        }
    } else if (queued_us < SONT_ABR_QUEUE_LOW_US) {
        if (++e->LdacCleanPackets >= SONT_LDAC_RECOVER_PACKETS && e->LdacEqmid > e->LdacMaxEqmid) {
            if (SontLdacSetEqmid(e->Ldac, (uint8_t)(e->LdacEqmid - 1u)) == 0) {
                SontTraceLog(ext, SonatinaEventAbrStep, e->LdacEqmid, (ULONG)e->LdacEqmid - 1u);
                e->LdacEqmid--;
            }
            e->LdacCleanPackets = 0;
        }
    } else {
        e->LdacCleanPackets = 0;
    }

    KeAcquireSpinLock(&ext->Lock, &old_irql);
    ext->State.LostPackets = e->Pull.underruns;
    ext->State.BitrateKbps = (ULONG)SontLdacBitrateKbpsInt(e->Rate, e->LdacEqmid);
    KeReleaseSpinLock(&ext->Lock, old_irql);
    return STATUS_SUCCESS;
}

/* Encodes and queues every codec frame that is due now. */
static NTSTATUS SontMediaTick(PSONT_DEVICE_EXTENSION ext, PSONT_SIGNALING_SCRATCH scratch)
{
    PSONT_MEDIA_ENGINE e = &scratch->engine;
    uint32_t due;
    uint32_t i;
    NTSTATUS status;

    if (!e->Active || ext->Transport.MediaChannel == NULL) {
        return STATUS_SUCCESS;
    }
    status = SontReapTx(ext, scratch);
    if (!NT_SUCCESS(status)) {
        return status;  /* the link is gone */
    }
    due = SontMediaClockFramesDue(&e->Clock, SontQueryTimestampUs(ext),
                                  e->SamplesPerPush, SONT_MEDIA_MAX_BURST);
    for (i = 0; i < due; i++) {
        SontMediaPullPcm(ext, scratch);
        if (e->Codec == SONT_AVDTP_CODEC_SBC) {
            size_t rtp_len = 0;
            int step = SontMediaPushPcmFrame(&scratch->media, scratch->pcm_frame,
                                             e->SamplesPerPush, scratch->rtp,
                                             sizeof(scratch->rtp), &rtp_len);
            if (step < 0) {
                return STATUS_UNSUCCESSFUL;
            }
            if (step == 1) {
                SontSendSbcPacket(ext, scratch, rtp_len);
            }
        } else {
            status = SontPushLdac(ext, scratch);
            if (!NT_SUCCESS(status)) {
                return status;
            }
        }
        SontMediaClockConsume(&e->Clock, e->SamplesPerPush);
    }
    return STATUS_SUCCESS;
}

/* ------------------------------------------------------------------ signalling */

/* Sends what the session produced and runs any follow-up action. */
static NTSTATUS SontHandleStep(PSONT_DEVICE_EXTENSION ext, PSONT_SIGNALING_SCRATCH scratch,
                               SONT_AVDTP_STEP_RESULT step, size_t tx_len,
                               ULONGLONG *cmd_deadline)
{
    PSONT_TRANSPORT t = &ext->Transport;
    PSONT_AVDTP_SESSION s = &scratch->session;
    NTSTATUS status;

    for (;;) {
        switch (step) {
        case SONT_AVDTP_STEP_SEND:
        case SONT_AVDTP_STEP_REPLY:
            if (tx_len != 0) {
                status = SontTransportSend(t, t->SignalingChannel, scratch->tx, (ULONG)tx_len);
                if (!NT_SUCCESS(status)) {
                    return status;
                }
            }
            if (step == SONT_AVDTP_STEP_SEND) {
                *cmd_deadline = SontNowMs(ext) + SONT_CMD_TIMEOUT_MS;
            }
            SontTraceLog(ext, SonatinaEventAvdtpState, (ULONG)s->state, (ULONG)s->selected_acp_seid);
            return STATUS_SUCCESS;

        case SONT_AVDTP_STEP_OPEN_MEDIA: {
            USHORT mtu = 0;
            /* AVDTP transport channel: same PSM as signalling, opened before Start. */
            status = SontTransportOpen(t, SONT_L2CAP_PSM_AVDTP, SONT_MEDIA_FLUSH_TO_MS,
                                       &ext->StopEvent, SONT_CONNECT_TIMEOUT_MS,
                                       &t->MediaChannel, &mtu);
            if (!NT_SUCCESS(status)) {
                return status;
            }
            t->MediaMtu = mtu;
            step = SontAvdtpSessionMediaReady(s, scratch->tx, sizeof(scratch->tx), &tx_len);
            continue;
        }

        case SONT_AVDTP_STEP_ERROR:
            SontRecordSignalingError(ext);
            if (s->state == SONT_AVDTP_STATE_FAILED) {
                DbgPrint("Sonatina: AVDTP procedure failed (err=0x%02X)\n", s->last_error);
                return STATUS_UNSUCCESSFUL;
            }
            scratch->StartRejected = TRUE;  /* stream stays open; retried later */
            return STATUS_SUCCESS;

        default:
            return STATUS_SUCCESS;
        }
    }
}

static VOID SontInitSessionFromProfile(PSONT_DEVICE_EXTENSION ext, PSONT_AVDTP_SESSION s)
{
    SONATINA_PROFILE prof;
    SONT_SBC_PARAMS pref_sbc;
    uint8_t pref_codec;
    KIRQL old_irql;

    KeAcquireSpinLock(&ext->Lock, &old_irql);
    prof = ext->Profile;
    KeReleaseSpinLock(&ext->Lock, old_irql);

    SontBuildSbcPreference(&prof, &pref_sbc);
    /* Raw PCM is not an A2DP codec on a real link: SBC is the mandatory one. */
    pref_codec = (prof.Codec == SonatinaCodecLdac) ? SONT_AVDTP_CODEC_LDAC : SONT_AVDTP_CODEC_SBC;
    SontAvdtpSessionInit(s, 1, pref_codec, &pref_sbc);
    if (pref_codec == SONT_AVDTP_CODEC_LDAC) {
        SONT_LDAC_PARAMS pref_ldac;
        pref_ldac.sampling = prof.LdacSampling;
        pref_ldac.channel_mode = (uint8_t)prof.LdacChannelMode;
        pref_ldac.eqmid = (uint8_t)prof.LdacEqmid;
        SontAvdtpSessionSetLdacPreference(s, &pref_ldac);
    }
    SontAvdtpSessionSetLocalCaps(s, (uint8_t)prof.SbcMaxBitpool,
                                 (pref_codec == SONT_AVDTP_CODEC_LDAC) ? 0x3Cu : 0u);
}

/* An open WaveRT stream is not enough (browsers and chat apps keep a silent
 * stream running): look for actual signal in the newest ~20 ms of the tap. */
static BOOLEAN SontPcmHasRealAudio(PSONT_DEVICE_EXTENSION ext)
{
    PSONT_PCM_SOURCE src = &ext->PcmSource;
    BOOLEAN active = FALSE;
    KIRQL old_irql;

    KeAcquireSpinLock(&src->Lock, &old_irql);
    if (src->StreamRunning) {
        active = SontPcmRingHasSignal(&src->Ring, SONT_SIGNAL_SCAN_BYTES, SONT_SIGNAL_THRESHOLD)
                     ? TRUE : FALSE;
    }
    KeReleaseSpinLock(&src->Lock, old_irql);
    return active;
}

static ULONGLONG SontMinDeadline(ULONGLONG wait_ms, ULONGLONG now, ULONGLONG at)
{
    ULONGLONG left;
    if (at == 0) {
        return wait_ms;
    }
    left = (at > now) ? (at - now) : 0;
    return (left < wait_ms) ? left : wait_ms;
}

/* Runs one signalling connection until it drops, fails or the worker stops.
 * The signalling channel is already open in ext->Transport.SignalingChannel. */
static NTSTATUS SontRunConnection(PSONT_DEVICE_EXTENSION ext, PSONT_SIGNALING_SCRATCH scratch,
                                  BOOLEAN remote_first, BOOLEAN *streamed)
{
    PSONT_TRANSPORT t = &ext->Transport;
    PSONT_AVDTP_SESSION s = &scratch->session;
    PVOID objs[4];
    ULONGLONG now;
    ULONGLONG hs_at;
    ULONGLONG cmd_deadline = 0;
    ULONGLONG resume_at = 0;
    ULONGLONG media_wait_at = 0;
    ULONGLONG resume_backoff = SONT_RESUME_MIN_MS;
    SONT_AVDTP_STEP_RESULT step;
    size_t tx_len = 0;
    NTSTATUS status;

    *streamed = FALSE;
    scratch->StartRejected = FALSE;
    SontInitSessionFromProfile(ext, s);
    (void)SontTransportTakeLost(t);

    status = SontAsyncRxStart(&scratch->rx, t->SignalingChannel);
    if (status != STATUS_PENDING) {
        SontTransportClose(t, &t->SignalingChannel);
        return NT_SUCCESS(status) ? STATUS_UNSUCCESSFUL : status;
    }
    status = STATUS_SUCCESS;

    /* After an incoming connection the headset usually drives the setup:
     * give it a moment before starting our own Discover. */
    now = SontNowMs(ext);
    hs_at = remote_first ? now + SONT_REMOTE_FIRST_MS : now;

    objs[0] = &ext->StopEvent;
    objs[1] = &scratch->rx.Done;
    objs[2] = &t->IncomingEvent;
    objs[3] = &t->LinkEvent;

    for (;;) {
        ULONGLONG wait_ms = SONT_IDLE_POLL_MS;
        LARGE_INTEGER due;
        NTSTATUS ws;
        uint32_t ev;

        /* ---- timers */
        now = SontNowMs(ext);
        if (hs_at != 0 && now >= hs_at) {
            hs_at = 0;
            if (!SontAvdtpSessionHasStream(s) && !SontAvdtpSessionIsWaiting(s)) {
                step = SontAvdtpSessionStartHandshake(s, scratch->tx, sizeof(scratch->tx), &tx_len);
                status = SontHandleStep(ext, scratch, step, tx_len, &cmd_deadline);
                if (!NT_SUCCESS(status)) {
                    break;
                }
            }
        }
        if (SontAvdtpSessionIsWaiting(s) && cmd_deadline != 0 && now >= cmd_deadline) {
            DbgPrint("Sonatina: AVDTP signal 0x%02X timed out\n", s->pending_signal);
            SontAvdtpSessionOnTimeout(s);
            status = STATUS_IO_TIMEOUT;
            break;
        }
        if (media_wait_at != 0 && now >= media_wait_at) {
            media_wait_at = 0;
            if (s->state == SONT_AVDTP_STATE_OPEN_WAIT_MEDIA && t->MediaChannel == NULL) {
                /* The headset sent Open but never opened the transport: do it ourselves. */
                status = SontHandleStep(ext, scratch, SONT_AVDTP_STEP_OPEN_MEDIA, 0, &cmd_deadline);
                if (!NT_SUCCESS(status)) {
                    break;
                }
            }
        }
        if (scratch->StartRejected) {
            scratch->StartRejected = FALSE;
            resume_at = now + resume_backoff;
            resume_backoff = (resume_backoff * 2 < SONT_RESUME_MAX_MS) ? resume_backoff * 2
                                                                      : SONT_RESUME_MAX_MS;
        }
        if (resume_at != 0 && now >= resume_at) {
            resume_at = 0;
            if (s->state == SONT_AVDTP_STATE_OPEN_PAUSED && s->media_open &&
                !SontAvdtpSessionIsWaiting(s)) {
                if (SontPcmHasRealAudio(ext)) {
                    /* Windows is really playing again: take the headset back. */
                    step = SontAvdtpSessionResume(s, scratch->tx, sizeof(scratch->tx), &tx_len);
                    status = SontHandleStep(ext, scratch, step, tx_len, &cmd_deadline);
                    if (!NT_SUCCESS(status)) {
                        break;
                    }
                } else {
                    resume_at = now + SONT_IDLE_POLL_MS;
                }
            }
        }

        /* ---- media */
        if (scratch->engine.Active && s->state == SONT_AVDTP_STATE_STREAMING) {
            status = SontMediaTick(ext, scratch);
            if (!NT_SUCCESS(status)) {
                DbgPrint("Sonatina: media send failing (0x%08X), reconnecting\n", status);
                break;
            }
            now = SontNowMs(ext);
            wait_ms = SontMediaClockUsUntilNext(&scratch->engine.Clock, SontQueryTimestampUs(ext),
                                                scratch->engine.SamplesPerPush) / 1000u;
            if (wait_ms > SONT_MEDIA_MAX_WAIT_MS) {
                wait_ms = SONT_MEDIA_MAX_WAIT_MS;
            }
        }
        wait_ms = SontMinDeadline(wait_ms, now, hs_at);
        wait_ms = SontMinDeadline(wait_ms, now, SontAvdtpSessionIsWaiting(s) ? cmd_deadline : 0);
        wait_ms = SontMinDeadline(wait_ms, now, resume_at);
        wait_ms = SontMinDeadline(wait_ms, now, media_wait_at);

        /* ---- wait */
        due.QuadPart = -(LONGLONG)wait_ms * 10000LL;
        ws = KeWaitForMultipleObjects(4, objs, WaitAny, Executive, KernelMode, FALSE,
                                      &due, scratch->wait_blocks);
        if (ws == STATUS_WAIT_0) {
            status = STATUS_CANCELLED;
            break;
        }

        /* ---- a channel dropped */
        if (KeReadStateEvent(&t->LinkEvent) != 0) {
            LONG lost = SontTransportTakeLost(t);
            if ((lost & SONT_LOST_SIGNALING) != 0) {
                status = STATUS_CONNECTION_DISCONNECTED;
                break;
            }
            if ((lost & SONT_LOST_MEDIA) != 0) {
                SontMediaStop(ext, scratch);
                SontTransportClose(t, &t->MediaChannel);
                if (SontAvdtpSessionHasStream(s)) {
                    status = STATUS_CONNECTION_DISCONNECTED;
                    break;
                }
            }
        }

        /* ---- incoming channel: the headset's transport channel after its Open */
        if (KeReadStateEvent(&t->IncomingEvent) != 0) {
            SONT_INCOMING inc;
            while (SontTransportTakeIncoming(t, &inc)) {
                if (inc.Psm == SONT_L2CAP_PSM_AVDTP && t->MediaChannel == NULL &&
                    s->state == SONT_AVDTP_STATE_OPEN_WAIT_MEDIA) {
                    USHORT mtu = 0;
                    status = SontTransportAccept(t, &inc, SONT_MEDIA_FLUSH_TO_MS,
                                                 &t->MediaChannel, &mtu);
                    if (!NT_SUCCESS(status)) {
                        break;
                    }
                    t->MediaMtu = mtu;
                    media_wait_at = 0;
                    step = SontAvdtpSessionMediaReady(s, scratch->tx, sizeof(scratch->tx), &tx_len);
                    status = SontHandleStep(ext, scratch, step, tx_len, &cmd_deadline);
                    if (!NT_SUCCESS(status)) {
                        break;
                    }
                } else {
                    SontTransportRefuse(t, &inc);
                }
            }
            if (!NT_SUCCESS(status)) {
                break;
            }
        }

        /* ---- signalling PDU */
        if (KeReadStateEvent(&scratch->rx.Done) != 0) {
            SontAsyncRxComplete(&scratch->rx);
            if (!NT_SUCCESS(scratch->rx.Status)) {
                status = scratch->rx.Status;
                break;
            }
            if (scratch->rx.Length > 0) {
                step = SontAvdtpSessionOnRx(s, scratch->rx.Buffer, scratch->rx.Length,
                                            scratch->tx, sizeof(scratch->tx), &tx_len);
                status = SontHandleStep(ext, scratch, step, tx_len, &cmd_deadline);
                if (!NT_SUCCESS(status)) {
                    break;
                }
            }
            if (SontAsyncRxStart(&scratch->rx, t->SignalingChannel) != STATUS_PENDING) {
                status = STATUS_UNSUCCESSFUL;
                break;
            }
        }

        /* ---- session events */
        now = SontNowMs(ext);
        ev = SontAvdtpSessionTakeEvents(s);
        if ((ev & SONT_AVDTP_EV_REMOTE_OPENED) != 0) {
            media_wait_at = now + SONT_MEDIA_OPEN_WAIT_MS;
        }
        if ((ev & SONT_AVDTP_EV_STARTED) != 0 && !scratch->engine.Active) {
            status = SontMediaStart(ext, scratch, t->MediaMtu);
            if (!NT_SUCCESS(status)) {
                break;
            }
            *streamed = TRUE;
            resume_backoff = SONT_RESUME_MIN_MS;
        }
        if ((ev & SONT_AVDTP_EV_SUSPENDED) != 0) {
            /* e.g. the phone took the headset over (multipoint). */
            SontMediaStop(ext, scratch);
            resume_at = now + resume_backoff;
        }
        if ((ev & (SONT_AVDTP_EV_CLOSED | SONT_AVDTP_EV_ABORTED)) != 0) {
            SontMediaStop(ext, scratch);
            SontTransportClose(t, &t->MediaChannel);
            resume_at = 0;
            hs_at = now + SONT_REHANDSHAKE_MS;  /* unless the headset reconfigures first */
        }
        if ((ev & SONT_AVDTP_EV_DELAY_REPORT) != 0) {
            KIRQL old_irql;
            KeAcquireSpinLock(&ext->Lock, &old_irql);
            ext->State.DelayMs = (ULONG)s->remote_delay_100us / 10u;
            KeReleaseSpinLock(&ext->Lock, old_irql);
        }
        if (s->state == SONT_AVDTP_STATE_FAILED) {
            status = STATUS_UNSUCCESSFUL;
            break;
        }
    }

    SontMediaStop(ext, scratch);
    if (ext->ReconnectRequested && SontAvdtpSessionHasStream(s) &&
        t->SignalingChannel != NULL && (t->LostMask & SONT_LOST_SIGNALING) == 0) {
        if (SontAvdtpSessionClose(s, scratch->tx, sizeof(scratch->tx), &tx_len) ==
            SONT_AVDTP_STEP_SEND) {
            (void)SontTransportSend(t, t->SignalingChannel, scratch->tx, (ULONG)tx_len);
        }
    }
    SontAsyncRxCancel(&scratch->rx);
    SontTransportClose(t, &t->MediaChannel);
    SontTransportClose(t, &t->SignalingChannel);
    (void)SontTransportTakeLost(t);
    return status;
}

static ULONGLONG SontRetryDelayMs(ULONG attempts)
{
    switch (attempts) {
    case 0:
    case 1:  return 2000;
    case 2:  return 5000;
    case 3:  return 10000;
    default: return 15000;
    }
}

/* Connection supervisor: accepts the headset's own reconnection, or connects
 * out (a few times after start, then only while Windows is playing audio),
 * and runs each connection until it drops. */
static VOID SontSignalingWorker(PVOID context)
{
    PSONT_DEVICE_EXTENSION ext = (PSONT_DEVICE_EXTENSION)context;
    PSONT_TRANSPORT t = &ext->Transport;
    PSONT_SIGNALING_SCRATCH scratch;
    PVOID objs[2];
    ULONG attempts = 0;
    ULONGLONG next_try;
    NTSTATUS status;

    scratch = (PSONT_SIGNALING_SCRATCH)ExAllocatePool2(POOL_FLAG_NON_PAGED, sizeof(*scratch),
                                                       SONT_POOL_TAG);
    if (scratch == NULL) {
        SontRecordSignalingError(ext);
        PsTerminateSystemThread(STATUS_INSUFFICIENT_RESOURCES);
        return;
    }
    status = SontAsyncRxInit(&scratch->rx, t);
    if (NT_SUCCESS(status)) {
        status = SontTxQueueInit(&scratch->txq, t);
        if (!NT_SUCCESS(status)) {
            SontAsyncRxFree(&scratch->rx);
        }
    }
    if (!NT_SUCCESS(status)) {
        ExFreePoolWithTag(scratch, SONT_POOL_TAG);
        SontRecordSignalingError(ext);
        PsTerminateSystemThread(status);
        return;
    }
    (void)SontTransportRegisterServer(t);

    objs[0] = &ext->StopEvent;
    objs[1] = &t->IncomingEvent;
    next_try = SontNowMs(ext);

    while (KeReadStateEvent(&ext->StopEvent) == 0) {
        SONT_INCOMING inc;
        BOOLEAN connected = FALSE;
        BOOLEAN remote_first = FALSE;
        BOOLEAN streamed = FALSE;
        ULONGLONG now = SontNowMs(ext);
        ULONGLONG wait_ms;
        LARGE_INTEGER due;

        if (SontTransportTakeIncoming(t, &inc)) {
            if (inc.Psm == SONT_L2CAP_PSM_AVDTP) {
                status = SontTransportAccept(t, &inc, 0xFFFF, &t->SignalingChannel, &t->SignalingMtu);
                connected = NT_SUCCESS(status);
                remote_first = TRUE;
            } else {
                SontTransportRefuse(t, &inc);
            }
        } else if (now >= next_try) {
            if (attempts >= SONT_EAGER_ATTEMPTS && !SontPcmHasRealAudio(ext)) {
                /* Headset probably off: wait for it to connect, or for audio. */
                next_try = now + SONT_IDLE_POLL_MS;
            } else {
                status = SontTransportOpen(t, SONT_L2CAP_PSM_AVDTP, 0xFFFF, &ext->StopEvent,
                                           SONT_CONNECT_TIMEOUT_MS, &t->SignalingChannel,
                                           &t->SignalingMtu);
                connected = NT_SUCCESS(status);
                attempts++;
                if (!connected) {
                    next_try = SontNowMs(ext) + SontRetryDelayMs(attempts);
                }
            }
        }

        if (connected) {
            SontTraceLog(ext, SonatinaEventReconnect, remote_first ? 1u : 0u, attempts);
            status = SontRunConnection(ext, scratch, remote_first, &streamed);
            DbgPrint("Sonatina: connection ended 0x%08X (streamed=%u)\n", status, (unsigned)streamed);
            if (status != STATUS_CANCELLED && status != STATUS_CONNECTION_DISCONNECTED) {
                SontRecordSignalingError(ext);
            }
            attempts = streamed ? 0u : attempts + 1u;
            next_try = SontNowMs(ext) + (streamed ? SONT_RECONNECT_AFTER_DROP_MS
                                                  : SontRetryDelayMs(attempts));
            continue;
        }

        now = SontNowMs(ext);
        wait_ms = (next_try > now) ? next_try - now : 0;
        if (wait_ms > SONT_IDLE_POLL_MS) {
            wait_ms = SONT_IDLE_POLL_MS;
        }
        due.QuadPart = -(LONGLONG)wait_ms * 10000LL;
        if (KeWaitForMultipleObjects(2, objs, WaitAny, Executive, KernelMode, FALSE,
                                     &due, NULL) == STATUS_WAIT_0) {
            break;
        }
    }

    SontTxQueueFree(&scratch->txq);
    SontAsyncRxFree(&scratch->rx);
    SontTransportUnregisterServer(t);
    {
        SONT_INCOMING leftover;
        while (SontTransportTakeIncoming(t, &leftover)) {
            SontTransportRefuse(t, &leftover);
        }
    }
    ExFreePoolWithTag(scratch, SONT_POOL_TAG);
    PsTerminateSystemThread(STATUS_SUCCESS);
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
