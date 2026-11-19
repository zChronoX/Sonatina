/* Minimal PortCls WaveRT render miniport & stream for Sonatina (D2: silence endpoint). */
#ifndef SONATINA_MINIPORT_WAVERT_H
#define SONATINA_MINIPORT_WAVERT_H

#include <ntddk.h>
#pragma warning(push)
#pragma warning(disable: 4115 4127 4201 4595)
#include <portcls.h>
#include <stdunk.h>
#pragma warning(pop)

#include "NewDelete.h"
#include "miniport_subdevices.h"

class CMiniportWaveRTStreamSonatina;
EXT_CALLBACK SontTimerNotifyCallback;

class CMiniportWaveRTSonatina :
    public IMiniportWaveRT,
    public CUnknown
{
private:
    volatile LONG m_ActiveStreams;
    PSONT_PCM_SOURCE m_PcmSource;

public:
    DECLARE_STD_UNKNOWN();
    CMiniportWaveRTSonatina(_In_opt_ PUNKNOWN UnknownOuter,
                            _In_opt_ PSONT_PCM_SOURCE PcmSource = NULL)
        : CUnknown(UnknownOuter),
          m_ActiveStreams(0),
          m_PcmSource(PcmSource)
    {
    }
    ~CMiniportWaveRTSonatina();

    IMP_IMiniportWaveRT;

    VOID NotifyStreamClosed();
    PSONT_PCM_SOURCE PcmSource() const { return m_PcmSource; }
};

typedef CMiniportWaveRTSonatina *PCMiniportWaveRTSonatina;

class CMiniportWaveRTStreamSonatina :
    public IDrmAudioStream,
    public IMiniportWaveRTStreamNotification,
    public CUnknown
{
private:
    CMiniportWaveRTSonatina *m_Miniport;
    /* Cached at Init (PASSIVE_LEVEL). DISPATCH_LEVEL paths (timer callback,
     * GetPosition, SetState) must use this, never m_Miniport->PcmSource():
     * a non-inlined accessor can be emitted in a PAGE section in Debug builds. */
    PSONT_PCM_SOURCE m_PcmSource;
    PPORTWAVERTSTREAM m_PortStream;
    BOOLEAN m_StreamRegistered;

    KSPIN_LOCK m_Lock;
    KSSTATE m_State;
    PEX_TIMER m_NotificationTimer;
    LIST_ENTRY m_NotificationList;

    PVOID m_DmaBuffer;
    ULONG m_DmaBufferSize;
    ULONG m_NotificationsPerBuffer;
    ULONGLONG m_NotificationIntervalHns;
    ULONGLONG m_ElapsedNotifyHns;

    ULONG m_AvgBytesPerSec;
    USHORT m_BlockAlign;
    ULONG m_SampleRate;
    ULONG m_Channels;

    LARGE_INTEGER m_QpcFrequency;
    LONGLONG m_LastDmaHns;
    ULONGLONG m_ByteRemainderHns;
    ULONGLONG m_PlayOffset;
    ULONGLONG m_WriteOffset;
    ULONGLONG m_LinearPosition;
    ULONG m_ContentId;

    friend EXT_CALLBACK SontTimerNotifyCallback;

    _IRQL_requires_(DISPATCH_LEVEL)
    VOID UpdatePositionLocked(_In_ LARGE_INTEGER QpcNow);

public:
    DECLARE_STD_UNKNOWN();
    CMiniportWaveRTStreamSonatina(_In_opt_ PUNKNOWN UnknownOuter);
    ~CMiniportWaveRTStreamSonatina();

    IMP_IMiniportWaveRTStream;
    IMP_IMiniportWaveRTStreamNotification;
    IMP_IDrmAudioStream;

    _IRQL_requires_max_(PASSIVE_LEVEL)
    NTSTATUS Init(
        _In_ CMiniportWaveRTSonatina *Miniport,
        _In_ PPORTWAVERTSTREAM PortStream,
        _In_ ULONG Pin,
        _In_ BOOLEAN Capture,
        _In_ const WAVEFORMATEX *WaveFormat
    );
};

_IRQL_requires_max_(PASSIVE_LEVEL)
NTSTATUS
SontCreateWaveRTMiniport(
    _In_opt_ PSONT_PCM_SOURCE PcmSource,
    _Outptr_ PUNKNOWN *Unknown
);

#endif /* SONATINA_MINIPORT_WAVERT_H */
