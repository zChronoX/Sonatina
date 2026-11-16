/* L2CAP transport built on the public Bluetooth profile-driver interface.
 *
 * A profile driver queries the Bluetooth stack for
 * GUID_BTHDDI_PROFILE_DRIVER_INTERFACE and submits BRBs to its parent device
 * with IRP_MJ_INTERNAL_DEVICE_CONTROL + IOCTL_INTERNAL_BTH_SUBMIT_BRB.
 *
 * AVDTP uses PSM 0x0019 for BOTH the signalling channel and the media
 * transport channel (the second channel opened on the same PSM after Open).
 * PSM 0x0017 is AVCTP (AVRCP remote control) and must not carry audio.
 *
 * Connections can be outgoing (BRB_L2CA_OPEN_CHANNEL) or incoming: the headset
 * usually reconnects on power-on, so the transport registers an L2CAP server
 * for its own address and queues incoming requests for the worker thread,
 * which answers them with BRB_L2CA_OPEN_CHANNEL_RESPONSE at PASSIVE_LEVEL.
 */
#ifndef SONATINA_BRB_TRANSPORT_H
#define SONATINA_BRB_TRANSPORT_H

#include <ntddk.h>
#include <bthdef.h>
#include <bthsdpdef.h>
#include <bthioctl.h>
#include <bthddi.h>
#include <bthguid.h>

#define SONT_L2CAP_PSM_AVDTP     0x0019
#define SONT_L2CAP_DEFAULT_MTU   672u
#define SONT_L2CAP_LOCAL_MTU     1024u  /* what we advertise we can receive */
#define SONT_TRANSPORT_MAX_INCOMING 4u
#define SONT_TRANSPORT_RX_CAP    1024u

#define SONT_LOST_SIGNALING 0x1L
#define SONT_LOST_MEDIA     0x2L

typedef struct _SONT_INCOMING {
    L2CAP_CHANNEL_HANDLE Handle;
    BTH_ADDR Address;
    USHORT Psm;
} SONT_INCOMING, *PSONT_INCOMING;

typedef struct _SONT_TRANSPORT {
    PDEVICE_OBJECT Parent;
    BTH_PROFILE_DRIVER_INTERFACE Bth;
    BOOLEAN HasBth;
    BTH_ADDR RemoteAddress;

    L2CAP_SERVER_HANDLE ServerHandle;
    BOOLEAN ServerRegistered;

    KSPIN_LOCK IndicationLock;          /* protects Incoming[] */
    SONT_INCOMING Incoming[SONT_TRANSPORT_MAX_INCOMING];
    ULONG IncomingCount;
    KEVENT IncomingEvent;               /* notification: Incoming[] not empty */
    KEVENT LinkEvent;                   /* notification: a channel was dropped by the remote */
    volatile LONG LostMask;             /* SONT_LOST_* */

    L2CAP_CHANNEL_HANDLE SignalingChannel;
    L2CAP_CHANNEL_HANDLE MediaChannel;
    USHORT SignalingMtu;
    USHORT MediaMtu;                    /* largest packet the headset accepts */
} SONT_TRANSPORT, *PSONT_TRANSPORT;

/* One outstanding asynchronous receive on a channel (owned by the worker). */
typedef struct _SONT_ASYNC_RX {
    PSONT_TRANSPORT Transport;
    PIRP Irp;
    struct _BRB_L2CA_ACL_TRANSFER *Brb;
    KEVENT Done;
    BOOLEAN Pending;
    NTSTATUS Status;
    ULONG Length;
    UCHAR Buffer[SONT_TRANSPORT_RX_CAP];
} SONT_ASYNC_RX, *PSONT_ASYNC_RX;

NTSTATUS SontTransportInitialize(PSONT_TRANSPORT transport, PDEVICE_OBJECT parent);

NTSTATUS SontTransportRegisterServer(PSONT_TRANSPORT transport);
VOID SontTransportUnregisterServer(PSONT_TRANSPORT transport);
BOOLEAN SontTransportTakeIncoming(PSONT_TRANSPORT transport, PSONT_INCOMING incoming);
NTSTATUS SontTransportAccept(PSONT_TRANSPORT transport, const SONT_INCOMING *incoming,
                             USHORT flush_to_ms, L2CAP_CHANNEL_HANDLE *channel, USHORT *mtu);
VOID SontTransportRefuse(PSONT_TRANSPORT transport, const SONT_INCOMING *incoming);

/* Outgoing channel; bounded by `timeout_ms` and cancelled when `stop_event` fires. */
NTSTATUS SontTransportOpen(PSONT_TRANSPORT transport, USHORT psm, USHORT flush_to_ms,
                           PKEVENT stop_event, ULONG timeout_ms,
                           L2CAP_CHANNEL_HANDLE *channel, USHORT *mtu);

NTSTATUS SontTransportSend(PSONT_TRANSPORT transport, L2CAP_CHANNEL_HANDLE channel,
                           const void *data, ULONG length);
LONG SontTransportTakeLost(PSONT_TRANSPORT transport);
VOID SontTransportClose(PSONT_TRANSPORT transport, L2CAP_CHANNEL_HANDLE *channel);
VOID SontTransportTeardown(PSONT_TRANSPORT transport);

NTSTATUS SontAsyncRxInit(PSONT_ASYNC_RX rx, PSONT_TRANSPORT transport);
NTSTATUS SontAsyncRxStart(PSONT_ASYNC_RX rx, L2CAP_CHANNEL_HANDLE channel);
/* Call after rx->Done is signalled: collects Status/Length, clears Pending. */
VOID SontAsyncRxComplete(PSONT_ASYNC_RX rx);
VOID SontAsyncRxCancel(PSONT_ASYNC_RX rx);
VOID SontAsyncRxFree(PSONT_ASYNC_RX rx);

#endif /* SONATINA_BRB_TRANSPORT_H */
