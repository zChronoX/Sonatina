/* Shared device state for SonatinaAudio.sys. */
#ifndef SONATINA_DEVICE_H
#define SONATINA_DEVICE_H

#include <ntddk.h>
#pragma warning(push)
#pragma warning(disable: 4115)
#include <portcls.h>
#pragma warning(pop)

#include "brb_transport.h"
#include "miniport_subdevices.h"
#include "public.h"
#include "trace_ring.h"

typedef struct _SONT_DEVICE_EXTENSION {
    PDEVICE_OBJECT PhysicalDeviceObject;
    PDEVICE_OBJECT LowerDeviceObject;
    UNICODE_STRING ControlInterfaceName;
    BOOLEAN ControlInterfaceRegistered;

    SONT_TRANSPORT Transport;
    BOOLEAN TransportReady;

    SONT_PCM_SOURCE PcmSource;

    KEVENT StopEvent;
    PVOID WorkerThread;
    KMUTEX WorkerMutex;
    volatile LONG Removing;
    BOOLEAN ReconnectRequested;

    LARGE_INTEGER QpcFreq;
    SONATINA_PROFILE Profile;
    SONATINA_STATE State;
    SONT_TRACE_RING TraceRing;
    KSPIN_LOCK Lock;
} SONT_DEVICE_EXTENSION, *PSONT_DEVICE_EXTENSION;

/* PortCls reserves the first PORT_CLASS_DEVICE_EXTENSION_SIZE bytes of
 * DeviceObject->DeviceExtension for its own state. */
#define SONT_GET_EXTENSION(device) \
    ((PSONT_DEVICE_EXTENSION)((PUCHAR)(device)->DeviceExtension + \
                              PORT_CLASS_DEVICE_EXTENSION_SIZE))

#endif /* SONATINA_DEVICE_H */
