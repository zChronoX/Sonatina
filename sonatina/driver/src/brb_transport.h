/* L2CAP transport built on the public Bluetooth profile-driver interface.
 *
 * A profile driver queries the Bluetooth stack for
 * GUID_BTHDDI_PROFILE_DRIVER_INTERFACE and submits BRBs to its parent device
 * with IRP_MJ_INTERNAL_DEVICE_CONTROL + IOCTL_INTERNAL_BTH_SUBMIT_BRB.
 */
#ifndef SONATINA_BRB_TRANSPORT_H
#define SONATINA_BRB_TRANSPORT_H

#include <ntddk.h>
#include <bthddi.h>

#define SONT_L2CAP_PSM_AVDTP 0x0019
#define SONT_L2CAP_PSM_MEDIA 0x0017

typedef struct _SONT_TRANSPORT {
    PDEVICE_OBJECT Parent;
    BTH_PROFILE_DRIVER_INTERFACE Bth;
    BOOLEAN HasBth;
    BTH_ADDR RemoteAddress;
    L2CAP_CHANNEL_HANDLE SignalingChannel;
    L2CAP_CHANNEL_HANDLE MediaChannel;
} SONT_TRANSPORT, *PSONT_TRANSPORT;

NTSTATUS SontTransportInitialize(PSONT_TRANSPORT transport, PDEVICE_OBJECT parent);
NTSTATUS SontTransportOpen(PSONT_TRANSPORT transport, USHORT psm,
                           PL2CAP_CHANNEL_HANDLE channel);
NTSTATUS SontTransportSend(PSONT_TRANSPORT transport, L2CAP_CHANNEL_HANDLE channel,
                           const void *data, ULONG length);
VOID SontTransportClose(PSONT_TRANSPORT transport, PL2CAP_CHANNEL_HANDLE channel);
VOID SontTransportTeardown(PSONT_TRANSPORT transport);

#endif /* SONATINA_BRB_TRANSPORT_H */
