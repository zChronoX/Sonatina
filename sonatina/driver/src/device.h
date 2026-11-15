/* Shared device state for SonatinaAudio.sys. */
#ifndef SONATINA_DEVICE_H
#define SONATINA_DEVICE_H

#include <ntddk.h>
#include <portcls.h>

#include "brb_transport.h"
#include "public.h"

typedef struct _SONT_DEVICE_EXTENSION {
    PDEVICE_OBJECT PhysicalDeviceObject;
    PDEVICE_OBJECT LowerDeviceObject;
    UNICODE_STRING ControlInterfaceName;
    BOOLEAN ControlInterfaceRegistered;

    SONT_TRANSPORT Transport;
    BOOLEAN TransportReady;

    SONATINA_PROFILE Profile;
    SONATINA_STATE State;
    KSPIN_LOCK Lock;
} SONT_DEVICE_EXTENSION, *PSONT_DEVICE_EXTENSION;

/* PortCls reserves the first PORT_CLASS_DEVICE_EXTENSION_SIZE bytes of
 * DeviceObject->DeviceExtension for its own state. */
#define SONT_GET_EXTENSION(device) \
    ((PSONT_DEVICE_EXTENSION)((PUCHAR)(device)->DeviceExtension + \
                              PORT_CLASS_DEVICE_EXTENSION_SIZE))

#endif /* SONATINA_DEVICE_H */
