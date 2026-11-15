/* Shared C/C++ boundary for registering PortCls WaveRT + Topology subdevices. */
#ifndef SONATINA_MINIPORT_SUBDEVICES_H
#define SONATINA_MINIPORT_SUBDEVICES_H

#include <ntddk.h>
#pragma warning(push)
#pragma warning(disable: 4115)
#include <portcls.h>
#pragma warning(pop)

#ifdef __cplusplus
extern "C" {
#endif

#include "media_stream.h"

#define SONT_PCM_STAGE_BYTES 32768u

typedef struct _SONT_PCM_SOURCE {
    KSPIN_LOCK Lock;
    KEVENT DataEvent;
    UCHAR *BackingBuffer;
    SONT_PCM_RING Ring;
    ULONG StreamSampleRate;
    ULONG StreamChannels;
    BOOLEAN StreamRunning;
} SONT_PCM_SOURCE, *PSONT_PCM_SOURCE;

_IRQL_requires_max_(PASSIVE_LEVEL)
NTSTATUS
SontRegisterAudioSubdevices(
    _In_ PDEVICE_OBJECT DeviceObject,
    _In_ PIRP Irp,
    _In_opt_ PRESOURCELIST ResourceList,
    _In_opt_ PSONT_PCM_SOURCE PcmSource
);

#ifdef __cplusplus
}
#endif

#endif /* SONATINA_MINIPORT_SUBDEVICES_H */
