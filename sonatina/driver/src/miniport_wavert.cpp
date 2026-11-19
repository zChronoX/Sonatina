/* Minimal PortCls WaveRT render miniport & stream for Sonatina (D2: silence endpoint).
 * Uses Microsoft SysVAD (MS-PL) pin/table conventions with lock-guarded notification
 * list management and pure integer QPC position progression.
 */
#include <initguid.h>
#include "miniport_wavert.h"
#include "miniport_topology.h"
#include "miniport_subdevices.h"
#include "simple.h"

#define SONT_WAVERT_POOLTAG 'anOS'
#define SONT_HNS_PER_SECOND 10000000ULL
#define SONT_HNS_PER_MS     10000LL

typedef struct _SONT_NOTIFICATION_ENTRY {
    LIST_ENTRY ListEntry;
    PKEVENT NotificationEvent;
} SONT_NOTIFICATION_ENTRY;

static KSDATARANGE_AUDIO SontWavePinDataRangesStream[] =
{
    {
        {
            sizeof(KSDATARANGE_AUDIO),
            0,
            0,
            0,
            STATICGUIDOF(KSDATAFORMAT_TYPE_AUDIO),
            STATICGUIDOF(KSDATAFORMAT_SUBTYPE_PCM),
            STATICGUIDOF(KSDATAFORMAT_SPECIFIER_WAVEFORMATEX)
        },
        2,      /* MaximumChannels */
        16,     /* MinimumBitsPerSample */
        16,     /* MaximumBitsPerSample */
        44100,  /* MinimumSampleFrequency */
        96000   /* MaximumSampleFrequency */
    }
};

static PKSDATARANGE SontWavePinDataRangePointersStream[] =
{
    PKSDATARANGE(&SontWavePinDataRangesStream[0])
};

static KSDATARANGE SontWavePinDataRangesBridge[] =
{
    {
        sizeof(KSDATARANGE),
        0,
        0,
        0,
        STATICGUIDOF(KSDATAFORMAT_TYPE_AUDIO),
        STATICGUIDOF(KSDATAFORMAT_SUBTYPE_ANALOG),
        STATICGUIDOF(KSDATAFORMAT_SPECIFIER_NONE)
    }
};

static PKSDATARANGE SontWavePinDataRangePointersBridge[] =
{
    &SontWavePinDataRangesBridge[0]
};

static PCPIN_DESCRIPTOR SontWaveMiniportPins[] =
{
    /* KSPIN_WAVE_RENDER3_SINK_SYSTEM (0) */
    {
        1,
        1,
        0,
        NULL,
        {
            0,
            NULL,
            0,
            NULL,
            ARRAYSIZE(SontWavePinDataRangePointersStream),
            SontWavePinDataRangePointersStream,
            KSPIN_DATAFLOW_IN,
            KSPIN_COMMUNICATION_SINK,
            &KSCATEGORY_AUDIO,
            NULL,
            0
        }
    },
    /* KSPIN_WAVE_RENDER3_SOURCE (1, bridge to Topology subdevice) */
    {
        0,
        0,
        0,
        NULL,
        {
            0,
            NULL,
            0,
            NULL,
            ARRAYSIZE(SontWavePinDataRangePointersBridge),
            SontWavePinDataRangePointersBridge,
            KSPIN_DATAFLOW_OUT,
            KSPIN_COMMUNICATION_NONE,
            &KSCATEGORY_AUDIO,
            NULL,
            0
        }
    }
};

static PCCONNECTION_DESCRIPTOR SontWaveMiniportConnections[] =
{
    {
        PCFILTER_NODE,
        KSPIN_WAVE_RENDER3_SINK_SYSTEM,
        PCFILTER_NODE,
        KSPIN_WAVE_RENDER3_SOURCE
    }
};

static PCFILTER_DESCRIPTOR SontWaveMiniportFilterDescriptor =
{
    0,
    NULL,
    sizeof(PCPIN_DESCRIPTOR),
    ARRAYSIZE(SontWaveMiniportPins),
    SontWaveMiniportPins,
    sizeof(PCNODE_DESCRIPTOR),
    0,
    NULL,
    ARRAYSIZE(SontWaveMiniportConnections),
    SontWaveMiniportConnections,
    0,
    NULL
};

#pragma code_seg("PAGE")
static NTSTATUS
SontValidatePcmFormat(
    _In_ PKSDATAFORMAT DataFormat,
    _Outptr_ const WAVEFORMATEX **OutWaveFormat
)
{
    PAGED_CODE();

    if (!DataFormat || !OutWaveFormat)
    {
        return STATUS_INVALID_PARAMETER;
    }

    *OutWaveFormat = NULL;

    if (!IsEqualGUIDAligned(DataFormat->MajorFormat, KSDATAFORMAT_TYPE_AUDIO) ||
        !IsEqualGUIDAligned(DataFormat->SubFormat, KSDATAFORMAT_SUBTYPE_PCM) ||
        !IsEqualGUIDAligned(DataFormat->Specifier, KSDATAFORMAT_SPECIFIER_WAVEFORMATEX))
    {
        return STATUS_INVALID_PARAMETER;
    }

    if (DataFormat->FormatSize < sizeof(KSDATAFORMAT_WAVEFORMATEX))
    {
        return STATUS_INVALID_PARAMETER;
    }

    const WAVEFORMATEX *wfx = reinterpret_cast<const WAVEFORMATEX *>(DataFormat + 1);
    if (wfx->wFormatTag == WAVE_FORMAT_EXTENSIBLE)
    {
        if (DataFormat->FormatSize < sizeof(KSDATAFORMAT_WAVEFORMATEXTENSIBLE) ||
            wfx->cbSize < (sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX)))
        {
            return STATUS_INVALID_PARAMETER;
        }

        const WAVEFORMATEXTENSIBLE *wfxe = reinterpret_cast<const WAVEFORMATEXTENSIBLE *>(wfx);
        if (!IsEqualGUIDAligned(wfxe->SubFormat, KSDATAFORMAT_SUBTYPE_PCM) ||
            wfxe->Samples.wValidBitsPerSample != 16)
        {
            return STATUS_INVALID_PARAMETER;
        }
    }
    else if (wfx->wFormatTag != WAVE_FORMAT_PCM)
    {
        return STATUS_INVALID_PARAMETER;
    }

    if (wfx->nChannels != 2 || wfx->wBitsPerSample != 16)
    {
        return STATUS_INVALID_PARAMETER;
    }

    if (wfx->nSamplesPerSec != 44100 &&
        wfx->nSamplesPerSec != 48000 &&
        wfx->nSamplesPerSec != 88200 &&
        wfx->nSamplesPerSec != 96000)
    {
        return STATUS_INVALID_PARAMETER;
    }

    USHORT expectedAlign = (USHORT)(wfx->nChannels * (wfx->wBitsPerSample / 8));
    ULONG expectedRate = wfx->nSamplesPerSec * expectedAlign;
    if (wfx->nBlockAlign != expectedAlign || wfx->nAvgBytesPerSec != expectedRate)
    {
        return STATUS_INVALID_PARAMETER;
    }

    *OutWaveFormat = wfx;
    return STATUS_SUCCESS;
}

/* =========================================================================
 * CMiniportWaveRTSonatina
 * ========================================================================= */

#pragma code_seg("PAGE")
CMiniportWaveRTSonatina::~CMiniportWaveRTSonatina()
{
    PAGED_CODE();
}

#pragma code_seg()
VOID
CMiniportWaveRTSonatina::NotifyStreamClosed()
{
    InterlockedExchange(&m_ActiveStreams, 0);
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTSonatina::NonDelegatingQueryInterface(
    _In_ REFIID Interface,
    _COM_Outptr_ PVOID *Object
)
{
    PAGED_CODE();

    if (!Object)
    {
        return STATUS_INVALID_PARAMETER;
    }

    if (IsEqualGUIDAligned(Interface, IID_IUnknown))
    {
        *Object = PVOID(PUNKNOWN(this));
    }
    else if (IsEqualGUIDAligned(Interface, IID_IMiniport))
    {
        *Object = PVOID(PMINIPORT(this));
    }
    else if (IsEqualGUIDAligned(Interface, IID_IMiniportWaveRT))
    {
        *Object = PVOID(PMINIPORTWAVERT(this));
    }
    else
    {
        *Object = NULL;
        return STATUS_INVALID_PARAMETER;
    }

    PUNKNOWN(*Object)->AddRef();
    return STATUS_SUCCESS;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTSonatina::GetDescription(
    _Out_ PPCFILTER_DESCRIPTOR *Description
)
{
    PAGED_CODE();

    if (!Description)
    {
        return STATUS_INVALID_PARAMETER;
    }

    *Description = &SontWaveMiniportFilterDescriptor;
    return STATUS_SUCCESS;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTSonatina::DataRangeIntersection(
    _In_ ULONG PinId,
    _In_ PKSDATARANGE ClientDataRange,
    _In_ PKSDATARANGE MyDataRange,
    _In_ ULONG OutputBufferLength,
    _Out_writes_bytes_to_opt_(OutputBufferLength, *ResultantFormatLength) PVOID ResultantFormat,
    _Out_ PULONG ResultantFormatLength
)
{
    PAGED_CODE();

    UNREFERENCED_PARAMETER(MyDataRange);

    if (!ResultantFormatLength || !ClientDataRange)
    {
        return STATUS_INVALID_PARAMETER;
    }

    *ResultantFormatLength = 0;

    if (PinId == KSPIN_WAVE_RENDER3_SOURCE)
    {
        return STATUS_NOT_IMPLEMENTED;
    }

    if (PinId != KSPIN_WAVE_RENDER3_SINK_SYSTEM)
    {
        return STATUS_INVALID_PARAMETER;
    }

    if (!IsEqualGUIDAligned(ClientDataRange->MajorFormat, KSDATAFORMAT_TYPE_AUDIO) &&
        !IsEqualGUIDAligned(ClientDataRange->MajorFormat, KSDATAFORMAT_TYPE_WILDCARD))
    {
        return STATUS_NO_MATCH;
    }

    if (!IsEqualGUIDAligned(ClientDataRange->SubFormat, KSDATAFORMAT_SUBTYPE_PCM) &&
        !IsEqualGUIDAligned(ClientDataRange->SubFormat, KSDATAFORMAT_SUBTYPE_WILDCARD))
    {
        return STATUS_NO_MATCH;
    }

    if (!IsEqualGUIDAligned(ClientDataRange->Specifier, KSDATAFORMAT_SPECIFIER_WAVEFORMATEX) &&
        !IsEqualGUIDAligned(ClientDataRange->Specifier, KSDATAFORMAT_SPECIFIER_WILDCARD))
    {
        return STATUS_NOT_IMPLEMENTED;
    }

    ULONG sampleRate = 48000;
    if (IsEqualGUIDAligned(ClientDataRange->Specifier, KSDATAFORMAT_SPECIFIER_WAVEFORMATEX))
    {
        if (ClientDataRange->FormatSize < sizeof(KSDATARANGE_AUDIO))
        {
            return STATUS_NO_MATCH;
        }

        PKSDATARANGE_AUDIO clientAudio = (PKSDATARANGE_AUDIO)ClientDataRange;
        if (clientAudio->MaximumChannels < 2)
        {
            return STATUS_NO_MATCH;
        }

        if (clientAudio->MinimumBitsPerSample > 16 || clientAudio->MaximumBitsPerSample < 16)
        {
            return STATUS_NO_MATCH;
        }

        ULONG lowFreq = max(44100UL, clientAudio->MinimumSampleFrequency);
        ULONG highFreq = min(96000UL, clientAudio->MaximumSampleFrequency);
        if (lowFreq > highFreq)
        {
            return STATUS_NO_MATCH;
        }

        if (lowFreq <= 48000UL && 48000UL <= highFreq)
        {
            sampleRate = 48000UL;
        }
        else if (lowFreq <= 44100UL && 44100UL <= highFreq)
        {
            sampleRate = 44100UL;
        }
        else if (lowFreq <= 96000UL && 96000UL <= highFreq)
        {
            sampleRate = highFreq;
        }
        else
        {
            return STATUS_NO_MATCH;
        }
    }

    ULONG requiredSize = sizeof(KSDATAFORMAT_WAVEFORMATEXTENSIBLE);
    *ResultantFormatLength = requiredSize;

    if (OutputBufferLength == 0)
    {
        return STATUS_BUFFER_OVERFLOW;
    }

    if (OutputBufferLength < requiredSize)
    {
        return STATUS_BUFFER_TOO_SMALL;
    }

    if (!ResultantFormat)
    {
        return STATUS_INVALID_PARAMETER;
    }

    PKSDATAFORMAT_WAVEFORMATEXTENSIBLE fmt =
        reinterpret_cast<PKSDATAFORMAT_WAVEFORMATEXTENSIBLE>(ResultantFormat);
    RtlZeroMemory(fmt, requiredSize);

    fmt->DataFormat.FormatSize = requiredSize;
    fmt->DataFormat.MajorFormat = KSDATAFORMAT_TYPE_AUDIO;
    fmt->DataFormat.SubFormat = KSDATAFORMAT_SUBTYPE_PCM;
    fmt->DataFormat.Specifier = KSDATAFORMAT_SPECIFIER_WAVEFORMATEX;

    fmt->WaveFormatExt.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE;
    fmt->WaveFormatExt.Format.nChannels = 2;
    fmt->WaveFormatExt.Format.nSamplesPerSec = sampleRate;
    fmt->WaveFormatExt.Format.nBlockAlign = 4;
    fmt->WaveFormatExt.Format.nAvgBytesPerSec = sampleRate * 4;
    fmt->WaveFormatExt.Format.wBitsPerSample = 16;
    fmt->WaveFormatExt.Format.cbSize = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
    fmt->WaveFormatExt.Samples.wValidBitsPerSample = 16;
    fmt->WaveFormatExt.dwChannelMask = KSAUDIO_SPEAKER_STEREO;
    fmt->WaveFormatExt.SubFormat = KSDATAFORMAT_SUBTYPE_PCM;

    return STATUS_SUCCESS;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTSonatina::Init(
    _In_ PUNKNOWN UnknownAdapter,
    _In_ PRESOURCELIST ResourceList,
    _In_ PPORTWAVERT Port
)
{
    PAGED_CODE();

    UNREFERENCED_PARAMETER(UnknownAdapter);
    UNREFERENCED_PARAMETER(ResourceList);

    if (!Port)
    {
        return STATUS_INVALID_PARAMETER;
    }

    return STATUS_SUCCESS;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTSonatina::NewStream(
    _Out_ PMINIPORTWAVERTSTREAM *OutStream,
    _In_ PPORTWAVERTSTREAM OuterUnknown,
    _In_ ULONG Pin,
    _In_ BOOLEAN Capture,
    _In_ PKSDATAFORMAT DataFormat
)
{
    PAGED_CODE();

    if (!OutStream || !OuterUnknown || !DataFormat)
    {
        return STATUS_INVALID_PARAMETER;
    }

    *OutStream = NULL;

    if (Capture || Pin != KSPIN_WAVE_RENDER3_SINK_SYSTEM)
    {
        return STATUS_NOT_SUPPORTED;
    }

    const WAVEFORMATEX *wfx = NULL;
    NTSTATUS status = SontValidatePcmFormat(DataFormat, &wfx);
    if (!NT_SUCCESS(status))
    {
        return status;
    }

    if (InterlockedCompareExchange(&m_ActiveStreams, 1, 0) != 0)
    {
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    CMiniportWaveRTStreamSonatina *stream =
        new (POOL_FLAG_NON_PAGED, SONT_WAVERT_POOLTAG) CMiniportWaveRTStreamSonatina(NULL);
    if (!stream)
    {
        NotifyStreamClosed();
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    stream->AddRef();
    status = stream->Init(this, OuterUnknown, Pin, Capture, wfx);
    if (NT_SUCCESS(status))
    {
        *OutStream = PMINIPORTWAVERTSTREAM(stream);
        (*OutStream)->AddRef();
    }

    stream->Release();
    return status;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTSonatina::GetDeviceDescription(
    _Out_ PDEVICE_DESCRIPTION DmaDeviceDescription
)
{
    PAGED_CODE();

    if (!DmaDeviceDescription)
    {
        return STATUS_INVALID_PARAMETER;
    }

    RtlZeroMemory(DmaDeviceDescription, sizeof(DEVICE_DESCRIPTION));
    DmaDeviceDescription->Master = TRUE;
    DmaDeviceDescription->ScatterGather = TRUE;
    DmaDeviceDescription->Dma32BitAddresses = TRUE;
    DmaDeviceDescription->InterfaceType = PCIBus;
    DmaDeviceDescription->MaximumLength = 0xFFFFFFFF;

    return STATUS_SUCCESS;
}

/* =========================================================================
 * CMiniportWaveRTStreamSonatina
 * ========================================================================= */

#pragma code_seg("PAGE")
CMiniportWaveRTStreamSonatina::CMiniportWaveRTStreamSonatina(
    _In_opt_ PUNKNOWN UnknownOuter
)
    : CUnknown(UnknownOuter),
      m_Miniport(NULL),
      m_PcmSource(NULL),
      m_PortStream(NULL),
      m_StreamRegistered(FALSE),
      m_State(KSSTATE_STOP),
      m_NotificationTimer(NULL),
      m_DmaBuffer(NULL),
      m_DmaBufferSize(0),
      m_NotificationsPerBuffer(0),
      m_NotificationIntervalHns(0),
      m_ElapsedNotifyHns(0),
      m_AvgBytesPerSec(0),
      m_BlockAlign(0),
      m_SampleRate(0),
      m_Channels(0),
      m_LastDmaHns(0),
      m_ByteRemainderHns(0),
      m_PlayOffset(0),
      m_WriteOffset(0),
      m_LinearPosition(0),
      m_ContentId(0)
{
    PAGED_CODE();
    KeInitializeSpinLock(&m_Lock);
    InitializeListHead(&m_NotificationList);
    m_QpcFrequency.QuadPart = 0;
}

#pragma code_seg("PAGE")
CMiniportWaveRTStreamSonatina::~CMiniportWaveRTStreamSonatina()
{
    PAGED_CODE();

    if (m_NotificationTimer)
    {
        ExDeleteTimer(m_NotificationTimer, TRUE, TRUE, NULL);
        m_NotificationTimer = NULL;
    }

    KeFlushQueuedDpcs();

    while (!IsListEmpty(&m_NotificationList))
    {
        PLIST_ENTRY entry = RemoveHeadList(&m_NotificationList);
        SONT_NOTIFICATION_ENTRY *node =
            CONTAINING_RECORD(entry, SONT_NOTIFICATION_ENTRY, ListEntry);
        ExFreePoolWithTag(node, SONT_WAVERT_POOLTAG);
    }

    if (m_Miniport)
    {
        if (m_StreamRegistered)
        {
            m_Miniport->NotifyStreamClosed();
            m_StreamRegistered = FALSE;
        }
        m_Miniport->Release();
        m_Miniport = NULL;
    }
}

/* Non-paged: takes the PCM source spin lock (DISPATCH_LEVEL). Paged callers
 * must never hold a spin lock themselves, since their own code may be paged out. */
#pragma code_seg()
static DECLSPEC_NOINLINE VOID
SontPcmSourceSetFormat(
    _In_opt_ PSONT_PCM_SOURCE Source,
    _In_ ULONG SampleRate,
    _In_ ULONG Channels
)
{
    KIRQL oldIrql;

    if (Source == NULL)
    {
        return;
    }
    KeAcquireSpinLock(&Source->Lock, &oldIrql);
    Source->StreamSampleRate = SampleRate;
    Source->StreamChannels = Channels;
    KeReleaseSpinLock(&Source->Lock, oldIrql);
}

#pragma code_seg("PAGE")
NTSTATUS
CMiniportWaveRTStreamSonatina::Init(
    _In_ CMiniportWaveRTSonatina *Miniport,
    _In_ PPORTWAVERTSTREAM PortStream,
    _In_ ULONG Pin,
    _In_ BOOLEAN Capture,
    _In_ const WAVEFORMATEX *WaveFormat
)
{
    PAGED_CODE();

    UNREFERENCED_PARAMETER(Pin);
    UNREFERENCED_PARAMETER(Capture);

    if (!Miniport || !PortStream || !WaveFormat)
    {
        return STATUS_INVALID_PARAMETER;
    }

    m_Miniport = Miniport;
    m_Miniport->AddRef();
    m_PcmSource = m_Miniport->PcmSource();
    m_StreamRegistered = TRUE;
    m_PortStream = PortStream;
    m_AvgBytesPerSec = WaveFormat->nAvgBytesPerSec;
    m_BlockAlign = WaveFormat->nBlockAlign;
    m_SampleRate = WaveFormat->nSamplesPerSec;
    m_Channels = WaveFormat->nChannels;

    /* Not inline: holding a spin lock raises to DISPATCH_LEVEL, and this
     * function lives in the PAGE section (Driver Verifier bugcheck 0xD1). */
    SontPcmSourceSetFormat(m_PcmSource, m_SampleRate, m_Channels);

    (VOID)KeQueryPerformanceCounter(&m_QpcFrequency);
    if (m_QpcFrequency.QuadPart <= 0)
    {
        return STATUS_UNSUCCESSFUL;
    }

    m_NotificationTimer = ExAllocateTimer(
        SontTimerNotifyCallback,
        this,
        EX_TIMER_HIGH_RESOLUTION
    );
    if (!m_NotificationTimer)
    {
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    return STATUS_SUCCESS;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTStreamSonatina::NonDelegatingQueryInterface(
    _In_ REFIID Interface,
    _COM_Outptr_ PVOID *Object
)
{
    PAGED_CODE();

    if (!Object)
    {
        return STATUS_INVALID_PARAMETER;
    }

    if (IsEqualGUIDAligned(Interface, IID_IUnknown))
    {
        *Object = PVOID(PUNKNOWN(PMINIPORTWAVERTSTREAM(this)));
    }
    else if (IsEqualGUIDAligned(Interface, IID_IMiniportWaveRTStream))
    {
        *Object = PVOID(PMINIPORTWAVERTSTREAM(this));
    }
    else if (IsEqualGUIDAligned(Interface, IID_IMiniportWaveRTStreamNotification))
    {
        *Object = PVOID(PMINIPORTWAVERTSTREAMNOTIFICATION(this));
    }
    else if (IsEqualGUIDAligned(Interface, IID_IDrmAudioStream))
    {
        *Object = PVOID(PDRMAUDIOSTREAM(this));
    }
    else
    {
        *Object = NULL;
        return STATUS_INVALID_PARAMETER;
    }

    PUNKNOWN(*Object)->AddRef();
    return STATUS_SUCCESS;
}

#pragma code_seg()
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTStreamSonatina::AllocateAudioBuffer(
    _In_ ULONG RequestedSize,
    _Out_ PMDL *AudioBufferMdl,
    _Out_ ULONG *ActualSize,
    _Out_ ULONG *OffsetFromFirstPage,
    _Out_ MEMORY_CACHING_TYPE *CacheType
)
{
    PAGED_CODE();

    if (!AudioBufferMdl || !ActualSize || !OffsetFromFirstPage || !CacheType)
    {
        return STATUS_INVALID_PARAMETER;
    }

    if (m_BlockAlign == 0 || RequestedSize < m_BlockAlign)
    {
        return STATUS_UNSUCCESSFUL;
    }

    RequestedSize -= RequestedSize % m_BlockAlign;

    PHYSICAL_ADDRESS highAddress;
    highAddress.HighPart = 0;
    highAddress.LowPart = MAXULONG;

    PMDL mdl = m_PortStream->AllocatePagesForMdl(highAddress, RequestedSize);
    if (!mdl)
    {
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    PVOID mapped = m_PortStream->MapAllocatedPages(mdl, MmCached);
    if (!mapped)
    {
        m_PortStream->FreePagesFromMdl(mdl);
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    KIRQL oldIrql;
    KeAcquireSpinLock(&m_Lock, &oldIrql);
    m_DmaBuffer = mapped;
    m_DmaBufferSize = RequestedSize;
    m_NotificationsPerBuffer = 0;
    m_NotificationIntervalHns = 0;
    KeReleaseSpinLock(&m_Lock, oldIrql);

    *AudioBufferMdl = mdl;
    *ActualSize = RequestedSize;
    *OffsetFromFirstPage = 0;
    *CacheType = MmCached;
    return STATUS_SUCCESS;
}

#pragma code_seg()
STDMETHODIMP_(VOID)
CMiniportWaveRTStreamSonatina::FreeAudioBuffer(
    _In_opt_ PMDL AudioBufferMdl,
    _In_ ULONG BufferSize
)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(BufferSize);

    PVOID mapped = NULL;
    KIRQL oldIrql;
    KeAcquireSpinLock(&m_Lock, &oldIrql);
    mapped = m_DmaBuffer;
    m_DmaBuffer = NULL;
    m_DmaBufferSize = 0;
    m_NotificationsPerBuffer = 0;
    m_NotificationIntervalHns = 0;
    KeReleaseSpinLock(&m_Lock, oldIrql);

    if (AudioBufferMdl)
    {
        if (mapped)
        {
            m_PortStream->UnmapAllocatedPages(mapped, AudioBufferMdl);
        }
        m_PortStream->FreePagesFromMdl(AudioBufferMdl);
    }
}

#pragma code_seg()
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTStreamSonatina::AllocateBufferWithNotification(
    _In_ ULONG NotificationCount,
    _In_ ULONG RequestedSize,
    _Out_ PMDL *AudioBufferMdl,
    _Out_ ULONG *ActualSize,
    _Out_ ULONG *OffsetFromFirstPage,
    _Out_ MEMORY_CACHING_TYPE *CacheType
)
{
    PAGED_CODE();

    if (!AudioBufferMdl || !ActualSize || !OffsetFromFirstPage || !CacheType)
    {
        return STATUS_INVALID_PARAMETER;
    }

    if (NotificationCount == 0 || (RequestedSize % NotificationCount) != 0)
    {
        return STATUS_INVALID_PARAMETER;
    }

    if (m_BlockAlign == 0 || m_AvgBytesPerSec == 0 || RequestedSize < m_BlockAlign)
    {
        return STATUS_UNSUCCESSFUL;
    }

    RequestedSize -= RequestedSize % m_BlockAlign;

    PHYSICAL_ADDRESS highAddress;
    highAddress.HighPart = 0;
    highAddress.LowPart = MAXULONG;

    PMDL mdl = m_PortStream->AllocatePagesForMdl(highAddress, RequestedSize);
    if (!mdl)
    {
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    PVOID mapped = m_PortStream->MapAllocatedPages(mdl, MmCached);
    if (!mapped)
    {
        m_PortStream->FreePagesFromMdl(mdl);
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    ULONGLONG intervalHns =
        ((ULONGLONG)RequestedSize * SONT_HNS_PER_SECOND) /
        ((ULONGLONG)m_AvgBytesPerSec * (ULONGLONG)NotificationCount);
    if (intervalHns < (ULONGLONG)SONT_HNS_PER_MS)
    {
        intervalHns = (ULONGLONG)SONT_HNS_PER_MS;
    }

    KIRQL oldIrql;
    KeAcquireSpinLock(&m_Lock, &oldIrql);
    m_DmaBuffer = mapped;
    m_DmaBufferSize = RequestedSize;
    m_NotificationsPerBuffer = NotificationCount;
    m_NotificationIntervalHns = intervalHns;
    m_ElapsedNotifyHns = 0;
    KeReleaseSpinLock(&m_Lock, oldIrql);

    *AudioBufferMdl = mdl;
    *ActualSize = RequestedSize;
    *OffsetFromFirstPage = 0;
    *CacheType = MmCached;
    return STATUS_SUCCESS;
}

#pragma code_seg()
STDMETHODIMP_(VOID)
CMiniportWaveRTStreamSonatina::FreeBufferWithNotification(
    _In_ PMDL AudioBufferMdl,
    _In_ ULONG BufferSize
)
{
    PAGED_CODE();
    FreeAudioBuffer(AudioBufferMdl, BufferSize);
}

#pragma code_seg()
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTStreamSonatina::RegisterNotificationEvent(
    _In_ PKEVENT NotificationEvent
)
{
    if (!NotificationEvent)
    {
        return STATUS_INVALID_PARAMETER;
    }

    SONT_NOTIFICATION_ENTRY *node =
        (SONT_NOTIFICATION_ENTRY *)ExAllocatePool2(
            POOL_FLAG_NON_PAGED,
            sizeof(SONT_NOTIFICATION_ENTRY),
            SONT_WAVERT_POOLTAG
        );
    if (!node)
    {
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    node->NotificationEvent = NotificationEvent;

    KIRQL oldIrql;
    KeAcquireSpinLock(&m_Lock, &oldIrql);

    for (PLIST_ENTRY cur = m_NotificationList.Flink; cur != &m_NotificationList; cur = cur->Flink)
    {
        SONT_NOTIFICATION_ENTRY *existing =
            CONTAINING_RECORD(cur, SONT_NOTIFICATION_ENTRY, ListEntry);
        if (existing->NotificationEvent == NotificationEvent)
        {
            KeReleaseSpinLock(&m_Lock, oldIrql);
            ExFreePoolWithTag(node, SONT_WAVERT_POOLTAG);
            return STATUS_UNSUCCESSFUL;
        }
    }

    InsertTailList(&m_NotificationList, &node->ListEntry);
    KeReleaseSpinLock(&m_Lock, oldIrql);
    return STATUS_SUCCESS;
}

#pragma code_seg()
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTStreamSonatina::UnregisterNotificationEvent(
    _In_ PKEVENT NotificationEvent
)
{
    if (!NotificationEvent)
    {
        return STATUS_INVALID_PARAMETER;
    }

    SONT_NOTIFICATION_ENTRY *found = NULL;
    KIRQL oldIrql;
    KeAcquireSpinLock(&m_Lock, &oldIrql);

    for (PLIST_ENTRY cur = m_NotificationList.Flink; cur != &m_NotificationList; cur = cur->Flink)
    {
        SONT_NOTIFICATION_ENTRY *entry =
            CONTAINING_RECORD(cur, SONT_NOTIFICATION_ENTRY, ListEntry);
        if (entry->NotificationEvent == NotificationEvent)
        {
            RemoveEntryList(cur);
            found = entry;
            break;
        }
    }

    KeReleaseSpinLock(&m_Lock, oldIrql);

    if (!found)
    {
        return STATUS_NOT_FOUND;
    }

    ExFreePoolWithTag(found, SONT_WAVERT_POOLTAG);
    return STATUS_SUCCESS;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTStreamSonatina::GetClockRegister(
    _Out_ PKSRTAUDIO_HWREGISTER Register
)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(Register);
    return STATUS_NOT_IMPLEMENTED;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTStreamSonatina::GetPositionRegister(
    _Out_ PKSRTAUDIO_HWREGISTER Register
)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(Register);
    return STATUS_NOT_IMPLEMENTED;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(VOID)
CMiniportWaveRTStreamSonatina::GetHWLatency(
    _Out_ PKSRTAUDIO_HWLATENCY Latency
)
{
    PAGED_CODE();
    if (Latency)
    {
        Latency->ChipsetDelay = 0;
        Latency->CodecDelay = 0;
        Latency->FifoSize = 0;
    }
}

#pragma code_seg()
_IRQL_requires_(DISPATCH_LEVEL)
VOID
CMiniportWaveRTStreamSonatina::UpdatePositionLocked(
    _In_ LARGE_INTEGER QpcNow
)
{
    if (m_QpcFrequency.QuadPart <= 0 || m_DmaBufferSize == 0 || m_AvgBytesPerSec == 0)
    {
        return;
    }

    LONGLONG nowHns = KSCONVERT_PERFORMANCE_TIME(m_QpcFrequency.QuadPart, QpcNow);
    if (nowHns <= m_LastDmaHns)
    {
        return;
    }

    ULONGLONG deltaHns = (ULONGLONG)(nowHns - m_LastDmaHns);
    m_LastDmaHns = nowHns;

    ULONGLONG wholeSec = deltaHns / SONT_HNS_PER_SECOND;
    ULONGLONG remHns = deltaHns % SONT_HNS_PER_SECOND;
    ULONGLONG num = remHns * (ULONGLONG)m_AvgBytesPerSec + m_ByteRemainderHns;
    ULONGLONG bytes = wholeSec * (ULONGLONG)m_AvgBytesPerSec + (num / SONT_HNS_PER_SECOND);
    m_ByteRemainderHns = num % SONT_HNS_PER_SECOND;

    if (m_BlockAlign > 1)
    {
        bytes -= bytes % m_BlockAlign;
    }

    PVOID dma = m_DmaBuffer;
    ULONG dmaSize = m_DmaBufferSize;
    ULONG oldPlayOffset = (ULONG)m_PlayOffset;

    m_LinearPosition += bytes;
    m_PlayOffset = (m_PlayOffset + bytes) % m_DmaBufferSize;
    m_WriteOffset = m_PlayOffset;

    if (m_NotificationIntervalHns > 0)
    {
        m_ElapsedNotifyHns += deltaHns;
    }

    PSONT_PCM_SOURCE src = m_PcmSource;
    if (src != NULL && dma != NULL && dmaSize > 0 && bytes > 0)
    {
        ULONG copyLen = (bytes > (ULONGLONG)dmaSize) ? dmaSize : (ULONG)bytes;
        ULONG startOffset = (bytes > (ULONGLONG)dmaSize)
            ? (ULONG)((m_PlayOffset + dmaSize - copyLen) % dmaSize)
            : oldPlayOffset;
        ULONG tail = dmaSize - startOffset;
        ULONG first = (copyLen < tail) ? copyLen : tail;
        const UCHAR *dmaBytes = (const UCHAR *)dma;

        KeAcquireSpinLockAtDpcLevel(&src->Lock);
        (void)SontPcmRingWrite(&src->Ring, dmaBytes + startOffset, first);
        if (copyLen > first)
        {
            (void)SontPcmRingWrite(&src->Ring, dmaBytes, copyLen - first);
        }
        if (SontPcmRingAvailable(&src->Ring) >= 512)
        {
            KeSetEvent(&src->DataEvent, IO_NO_INCREMENT, FALSE);
        }
        KeReleaseSpinLockFromDpcLevel(&src->Lock);
    }
}

#pragma code_seg()
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTStreamSonatina::GetPosition(
    _Out_ PKSAUDIO_POSITION Position
)
{
    if (!Position)
    {
        return STATUS_INVALID_PARAMETER;
    }

    KIRQL oldIrql;
    KeAcquireSpinLock(&m_Lock, &oldIrql);

    if (m_State == KSSTATE_RUN)
    {
        LARGE_INTEGER qpc = KeQueryPerformanceCounter(NULL);
        UpdatePositionLocked(qpc);
    }

    Position->PlayOffset = m_PlayOffset;
    Position->WriteOffset = m_WriteOffset;

    KeReleaseSpinLock(&m_Lock, oldIrql);
    return STATUS_SUCCESS;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTStreamSonatina::SetFormat(
    _In_ PKSDATAFORMAT DataFormat
)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(DataFormat);
    return STATUS_NOT_SUPPORTED;
}

#pragma code_seg()
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTStreamSonatina::SetState(
    _In_ KSSTATE State
)
{
    KIRQL oldIrql;
    KSSTATE prevState;
    BOOLEAN startTimer = FALSE;
    BOOLEAN stopTimer = FALSE;
    LARGE_INTEGER qpc = KeQueryPerformanceCounter(NULL);

    KeAcquireSpinLock(&m_Lock, &oldIrql);
    prevState = m_State;

    switch (State)
    {
        case KSSTATE_STOP:
            if (prevState == KSSTATE_RUN)
            {
                UpdatePositionLocked(qpc);
                stopTimer = TRUE;
            }
            m_State = KSSTATE_STOP;
            m_PlayOffset = 0;
            m_WriteOffset = 0;
            m_LinearPosition = 0;
            m_ElapsedNotifyHns = 0;
            m_ByteRemainderHns = 0;
            m_LastDmaHns = 0;
            if (m_PcmSource != NULL)
            {
                PSONT_PCM_SOURCE src = m_PcmSource;
                KeAcquireSpinLockAtDpcLevel(&src->Lock);
                src->StreamRunning = FALSE;
                SontPcmRingReset(&src->Ring);
                KeClearEvent(&src->DataEvent);
                KeReleaseSpinLockFromDpcLevel(&src->Lock);
            }
            break;

        case KSSTATE_ACQUIRE:
            m_State = KSSTATE_ACQUIRE;
            break;

        case KSSTATE_PAUSE:
            if (prevState == KSSTATE_RUN)
            {
                UpdatePositionLocked(qpc);
                stopTimer = TRUE;
            }
            m_State = KSSTATE_PAUSE;
            if (m_PcmSource != NULL)
            {
                PSONT_PCM_SOURCE src = m_PcmSource;
                KeAcquireSpinLockAtDpcLevel(&src->Lock);
                src->StreamRunning = FALSE;
                KeReleaseSpinLockFromDpcLevel(&src->Lock);
            }
            break;

        case KSSTATE_RUN:
            if (m_QpcFrequency.QuadPart > 0)
            {
                m_LastDmaHns = KSCONVERT_PERFORMANCE_TIME(m_QpcFrequency.QuadPart, qpc);
            }
            m_State = KSSTATE_RUN;
            if (m_PcmSource != NULL)
            {
                PSONT_PCM_SOURCE src = m_PcmSource;
                KeAcquireSpinLockAtDpcLevel(&src->Lock);
                src->StreamSampleRate = m_SampleRate;
                src->StreamChannels = m_Channels;
                src->StreamRunning = TRUE;
                KeReleaseSpinLockFromDpcLevel(&src->Lock);
            }
            if (prevState != KSSTATE_RUN &&
                (m_NotificationsPerBuffer > 0 || m_PcmSource != NULL))
            {
                startTimer = TRUE;
            }
            break;

        default:
            KeReleaseSpinLock(&m_Lock, oldIrql);
            return STATUS_INVALID_PARAMETER;
    }

    KeReleaseSpinLock(&m_Lock, oldIrql);

    if (stopTimer && m_NotificationTimer)
    {
        ExCancelTimer(m_NotificationTimer, NULL);
        KeFlushQueuedDpcs();
    }
    else if (startTimer && m_NotificationTimer)
    {
        ExSetTimer(m_NotificationTimer, -SONT_HNS_PER_MS, SONT_HNS_PER_MS, NULL);
    }

    return STATUS_SUCCESS;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportWaveRTStreamSonatina::SetContentId(
    _In_ ULONG ContentId,
    _In_ PCDRMRIGHTS DrmRights
)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(DrmRights);
    m_ContentId = ContentId;
    return STATUS_SUCCESS;
}

#pragma code_seg()
VOID
SontTimerNotifyCallback(
    _In_ PEX_TIMER Timer,
    _In_opt_ PVOID Context
)
{
    UNREFERENCED_PARAMETER(Timer);

    CMiniportWaveRTStreamSonatina *stream =
        reinterpret_cast<CMiniportWaveRTStreamSonatina *>(Context);
    if (!stream)
    {
        return;
    }

    KIRQL oldIrql;
    KeAcquireSpinLock(&stream->m_Lock, &oldIrql);

    if (stream->m_State == KSSTATE_RUN)
    {
        LARGE_INTEGER qpc = KeQueryPerformanceCounter(NULL);
        stream->UpdatePositionLocked(qpc);

        if (stream->m_NotificationIntervalHns > 0 &&
            stream->m_ElapsedNotifyHns >= stream->m_NotificationIntervalHns)
        {
            stream->m_ElapsedNotifyHns %= stream->m_NotificationIntervalHns;

            for (PLIST_ENTRY cur = stream->m_NotificationList.Flink;
                 cur != &stream->m_NotificationList;
                 cur = cur->Flink)
            {
                SONT_NOTIFICATION_ENTRY *entry =
                    CONTAINING_RECORD(cur, SONT_NOTIFICATION_ENTRY, ListEntry);
                KeSetEvent(entry->NotificationEvent, 0, FALSE);
            }
        }
    }

    KeReleaseSpinLock(&stream->m_Lock, oldIrql);
}

#pragma code_seg("PAGE")
NTSTATUS
SontCreateWaveRTMiniport(
    _In_opt_ PSONT_PCM_SOURCE PcmSource,
    _Outptr_ PUNKNOWN *Unknown
)
{
    PAGED_CODE();

    if (!Unknown)
    {
        return STATUS_INVALID_PARAMETER;
    }

    *Unknown = NULL;
    CMiniportWaveRTSonatina *obj =
        new (POOL_FLAG_NON_PAGED, SONT_WAVERT_POOLTAG) CMiniportWaveRTSonatina(NULL, PcmSource);
    if (!obj)
    {
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    obj->AddRef();
    *Unknown = PUNKNOWN(obj);
    return STATUS_SUCCESS;
}

/* =========================================================================
 * Subdevice Registration (C-callable from SontStartDevice)
 * ========================================================================= */

#pragma code_seg("PAGE")
NTSTATUS
SontRegisterAudioSubdevices(
    _In_ PDEVICE_OBJECT DeviceObject,
    _In_ PIRP Irp,
    _In_opt_ PRESOURCELIST ResourceList,
    _In_opt_ PSONT_PCM_SOURCE PcmSource
)
{
    PAGED_CODE();

    if (!DeviceObject || !Irp)
    {
        return STATUS_INVALID_PARAMETER;
    }

    PPORT topoPort = NULL;
    PPORT wavePort = NULL;
    PUNKNOWN topoMiniport = NULL;
    PUNKNOWN waveMiniport = NULL;
    NTSTATUS status;

    status = PcNewPort(&topoPort, CLSID_PortTopology);
    if (!NT_SUCCESS(status))
    {
        goto Cleanup;
    }

    status = SontCreateTopologyMiniport(&topoMiniport);
    if (!NT_SUCCESS(status))
    {
        goto Cleanup;
    }

#pragma warning(push)
#pragma warning(disable: 6387)
    status = topoPort->Init(DeviceObject, Irp, topoMiniport, NULL, ResourceList);
#pragma warning(pop)
    if (!NT_SUCCESS(status))
    {
        goto Cleanup;
    }

    status = PcRegisterSubdevice(DeviceObject, L"Topology", topoPort);
    if (!NT_SUCCESS(status))
    {
        goto Cleanup;
    }

    status = PcNewPort(&wavePort, CLSID_PortWaveRT);
    if (!NT_SUCCESS(status))
    {
        goto Cleanup;
    }

    status = SontCreateWaveRTMiniport(PcmSource, &waveMiniport);
    if (!NT_SUCCESS(status))
    {
        goto Cleanup;
    }

#pragma warning(push)
#pragma warning(disable: 6387)
    status = wavePort->Init(DeviceObject, Irp, waveMiniport, NULL, ResourceList);
#pragma warning(pop)
    if (!NT_SUCCESS(status))
    {
        goto Cleanup;
    }

    status = PcRegisterSubdevice(DeviceObject, L"Wave", wavePort);
    if (!NT_SUCCESS(status))
    {
        goto Cleanup;
    }

    status = PcRegisterPhysicalConnection(
        DeviceObject,
        wavePort,
        KSPIN_WAVE_RENDER3_SOURCE,
        topoPort,
        KSPIN_TOPO_WAVEOUT_SOURCE
    );

Cleanup:
    if (waveMiniport)
    {
        waveMiniport->Release();
    }
    if (wavePort)
    {
        wavePort->Release();
    }
    if (topoMiniport)
    {
        topoMiniport->Release();
    }
    if (topoPort)
    {
        topoPort->Release();
    }

    return status;
}
