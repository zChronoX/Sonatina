#include "brb_transport.h"

#define SONT_POOL_TAG 'anOS'   /* "SOna" */

static NTSTATUS SontSyncCompletion(PDEVICE_OBJECT DeviceObject, PIRP Irp, PVOID Context)
{
    UNREFERENCED_PARAMETER(DeviceObject);
    UNREFERENCED_PARAMETER(Irp);
    KeSetEvent((PKEVENT)Context, IO_NO_INCREMENT, FALSE);
    return STATUS_MORE_PROCESSING_REQUIRED;
}

/* Fill `iface` with the Bluetooth profile-driver interface from the parent. */
static NTSTATUS SontQueryBthInterface(PDEVICE_OBJECT parent,
                                      PBTH_PROFILE_DRIVER_INTERFACE iface)
{
    PIRP irp;
    PIO_STACK_LOCATION stack;
    KEVENT event;
    NTSTATUS status;

    KeInitializeEvent(&event, NotificationEvent, FALSE);
    irp = IoAllocateIrp(parent->StackSize, FALSE);
    if (irp == NULL) {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    irp->IoStatus.Status = STATUS_NOT_SUPPORTED;  /* required for PnP IRPs */
    stack = IoGetNextIrpStackLocation(irp);
    stack->MajorFunction = IRP_MJ_PNP;
    stack->MinorFunction = IRP_MN_QUERY_INTERFACE;
    stack->Parameters.QueryInterface.InterfaceType = &GUID_BTHDDI_PROFILE_DRIVER_INTERFACE;
    stack->Parameters.QueryInterface.Size = (USHORT)sizeof(BTH_PROFILE_DRIVER_INTERFACE);
    stack->Parameters.QueryInterface.Version = BTHDDI_PROFILE_DRIVER_INTERFACE_VERSION_FOR_QI;
    stack->Parameters.QueryInterface.Interface = (PINTERFACE)iface;
    stack->Parameters.QueryInterface.InterfaceSpecificData = NULL;

    IoSetCompletionRoutine(irp, SontSyncCompletion, &event, TRUE, TRUE, TRUE);
    status = IoCallDriver(parent, irp);
    if (status == STATUS_PENDING) {
        KeWaitForSingleObject(&event, Executive, KernelMode, FALSE, NULL);
    }
    status = irp->IoStatus.Status;
    IoFreeIrp(irp);
    return status;
}

/* Read the remote Bluetooth address from the BTHENUM parent. */
static NTSTATUS SontQueryRemoteAddress(PDEVICE_OBJECT parent, BTH_ADDR *address)
{
    BTH_DEVICE_INFO info;
    KEVENT event;
    IO_STATUS_BLOCK iosb;
    PIRP irp;
    NTSTATUS status;

    RtlZeroMemory(&info, sizeof(info));
    KeInitializeEvent(&event, NotificationEvent, FALSE);
    irp = IoBuildDeviceIoControlRequest(IOCTL_INTERNAL_BTHENUM_GET_DEVINFO, parent,
                                        NULL, 0, &info, sizeof(info), TRUE,
                                        &event, &iosb);
    if (irp == NULL) {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    status = IoCallDriver(parent, irp);
    if (status == STATUS_PENDING) {
        KeWaitForSingleObject(&event, Executive, KernelMode, FALSE, NULL);
        status = iosb.Status;
    }
    if (NT_SUCCESS(status)) {
        *address = info.address;
    }
    return status;
}

/* Submit one BRB synchronously, optionally bounded by `stop_event` and `timeout_ms`.
 * The caller frees the BRB afterwards. */
static NTSTATUS SontSubmitBrbTimed(PDEVICE_OBJECT parent, PVOID brb,
                                   PKEVENT stop_event, ULONG timeout_ms)
{
    PIRP irp;
    PIO_STACK_LOCATION stack;
    KEVENT event;
    NTSTATUS status;

    KeInitializeEvent(&event, NotificationEvent, FALSE);
    irp = IoAllocateIrp(parent->StackSize, FALSE);
    if (irp == NULL) {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    stack = IoGetNextIrpStackLocation(irp);
    stack->MajorFunction = IRP_MJ_INTERNAL_DEVICE_CONTROL;
    stack->Parameters.DeviceIoControl.IoControlCode = IOCTL_INTERNAL_BTH_SUBMIT_BRB;
    stack->Parameters.Others.Argument1 = brb;

    IoSetCompletionRoutine(irp, SontSyncCompletion, &event, TRUE, TRUE, TRUE);
    status = IoCallDriver(parent, irp);
    if (status == STATUS_PENDING) {
        PVOID objects[2];
        ULONG count = 1;
        LARGE_INTEGER due;
        LARGE_INTEGER *p_due = NULL;
        NTSTATUS wait_status;

        objects[0] = &event;
        if (stop_event != NULL) {
            objects[1] = stop_event;
            count = 2;
        }
        if (timeout_ms != 0) {
            due.QuadPart = -(LONGLONG)timeout_ms * 10000LL;
            p_due = &due;
        }

        wait_status = KeWaitForMultipleObjects(count, objects, WaitAny,
                                               Executive, KernelMode, FALSE,
                                               p_due, NULL);
        if (wait_status == STATUS_TIMEOUT || wait_status == STATUS_WAIT_1) {
            (void)IoCancelIrp(irp);
            KeWaitForSingleObject(&event, Executive, KernelMode, FALSE, NULL);
            status = irp->IoStatus.Status;
            if (status == STATUS_CANCELLED && wait_status == STATUS_TIMEOUT) {
                status = STATUS_IO_TIMEOUT;
            }
            IoFreeIrp(irp);
            return status;
        }
    }
    status = irp->IoStatus.Status;
    IoFreeIrp(irp);
    return status;
}

static NTSTATUS SontSubmitBrb(PDEVICE_OBJECT parent, PVOID brb)
{
    return SontSubmitBrbTimed(parent, brb, NULL, 0);
}

/* ------------------------------------------------------------------ indications
 * Both callbacks may run at DISPATCH_LEVEL: they only record and signal. */

static void SontChannelCallback(PVOID context, INDICATION_CODE indication,
                                PINDICATION_PARAMETERS parameters)
{
    PSONT_TRANSPORT transport = (PSONT_TRANSPORT)context;
    LONG bit;

    if (transport == NULL || indication != IndicationRemoteDisconnect || parameters == NULL) {
        return;
    }
    bit = (parameters->ConnectionHandle == transport->MediaChannel) ? SONT_LOST_MEDIA
                                                                    : SONT_LOST_SIGNALING;
    InterlockedOr(&transport->LostMask, bit);
    KeSetEvent(&transport->LinkEvent, IO_NO_INCREMENT, FALSE);
}

static void SontServerCallback(PVOID context, INDICATION_CODE indication,
                               PINDICATION_PARAMETERS parameters)
{
    PSONT_TRANSPORT transport = (PSONT_TRANSPORT)context;
    KIRQL old_irql;

    if (transport == NULL || parameters == NULL || indication != IndicationRemoteConnect) {
        return;
    }
    KeAcquireSpinLock(&transport->IndicationLock, &old_irql);
    if (transport->IncomingCount < SONT_TRANSPORT_MAX_INCOMING) {
        PSONT_INCOMING slot = &transport->Incoming[transport->IncomingCount++];
        slot->Handle = parameters->ConnectionHandle;
        slot->Address = parameters->BtAddress;
        slot->Psm = parameters->Parameters.Connect.Request.PSM;
    }
    KeSetEvent(&transport->IncomingEvent, IO_NO_INCREMENT, FALSE);
    KeReleaseSpinLock(&transport->IndicationLock, old_irql);
}

/* ------------------------------------------------------------------ setup */

NTSTATUS SontTransportInitialize(PSONT_TRANSPORT transport, PDEVICE_OBJECT parent)
{
    NTSTATUS status;

    if (transport == NULL || parent == NULL) {
        return STATUS_INVALID_PARAMETER;
    }
    RtlZeroMemory(transport, sizeof(*transport));
    transport->Parent = parent;
    KeInitializeSpinLock(&transport->IndicationLock);
    KeInitializeEvent(&transport->IncomingEvent, NotificationEvent, FALSE);
    KeInitializeEvent(&transport->LinkEvent, NotificationEvent, FALSE);

    status = SontQueryBthInterface(parent, &transport->Bth);
    if (!NT_SUCCESS(status)) {
        DbgPrint("Sonatina: QUERY_INTERFACE(BTHDDI) failed 0x%08X\n", status);
        return status;
    }
    if (transport->Bth.BthAllocateBrb == NULL || transport->Bth.BthFreeBrb == NULL ||
        transport->Bth.BthReuseBrb == NULL) {
        return STATUS_UNSUCCESSFUL;
    }
    transport->HasBth = TRUE;

    status = SontQueryRemoteAddress(parent, &transport->RemoteAddress);
    if (!NT_SUCCESS(status)) {
        DbgPrint("Sonatina: BTHENUM GET_DEVINFO failed 0x%08X\n", status);
        return status;
    }
    DbgPrint("Sonatina: remote address %llX\n", (unsigned long long)transport->RemoteAddress);
    return STATUS_SUCCESS;
}

NTSTATUS SontTransportRegisterServer(PSONT_TRANSPORT transport)
{
    struct _BRB_L2CA_REGISTER_SERVER *brb;
    NTSTATUS status;

    if (transport == NULL || !transport->HasBth) {
        return STATUS_INVALID_PARAMETER;
    }
    if (transport->ServerRegistered) {
        return STATUS_SUCCESS;
    }
    brb = (struct _BRB_L2CA_REGISTER_SERVER *)transport->Bth.BthAllocateBrb(
        BRB_L2CA_REGISTER_SERVER, SONT_POOL_TAG);
    if (brb == NULL) {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    /* Specific device/PSM pair: only this headset, only AVDTP. */
    brb->BtAddress = transport->RemoteAddress;
    brb->PSM = SONT_L2CAP_PSM_AVDTP;
    brb->IndicationFlags = 0;
    brb->IndicationCallback = SontServerCallback;
    brb->IndicationCallbackContext = transport;
    brb->ReferenceObject = transport->Parent;

    status = SontSubmitBrb(transport->Parent, brb);
    if (NT_SUCCESS(status)) {
        transport->ServerHandle = brb->ServerHandle;
        transport->ServerRegistered = TRUE;
    } else {
        DbgPrint("Sonatina: BRB_L2CA_REGISTER_SERVER(0x19) failed 0x%08X; outgoing only\n", status);
    }
    transport->Bth.BthFreeBrb((PBRB)brb);
    return status;
}

VOID SontTransportUnregisterServer(PSONT_TRANSPORT transport)
{
    struct _BRB_L2CA_UNREGISTER_SERVER *brb;

    if (transport == NULL || !transport->HasBth || !transport->ServerRegistered) {
        return;
    }
    brb = (struct _BRB_L2CA_UNREGISTER_SERVER *)transport->Bth.BthAllocateBrb(
        BRB_L2CA_UNREGISTER_SERVER, SONT_POOL_TAG);
    if (brb != NULL) {
        brb->BtAddress = transport->RemoteAddress;
        brb->Psm = SONT_L2CAP_PSM_AVDTP;
        brb->ServerHandle = transport->ServerHandle;
        (void)SontSubmitBrb(transport->Parent, brb);
        transport->Bth.BthFreeBrb((PBRB)brb);
    }
    transport->ServerRegistered = FALSE;
    transport->ServerHandle = NULL;
}

BOOLEAN SontTransportTakeIncoming(PSONT_TRANSPORT transport, PSONT_INCOMING incoming)
{
    KIRQL old_irql;
    BOOLEAN got = FALSE;
    ULONG i;

    if (transport == NULL || incoming == NULL) {
        return FALSE;
    }
    KeAcquireSpinLock(&transport->IndicationLock, &old_irql);
    if (transport->IncomingCount > 0) {
        *incoming = transport->Incoming[0];
        for (i = 1; i < transport->IncomingCount; i++) {
            transport->Incoming[i - 1] = transport->Incoming[i];
        }
        transport->IncomingCount--;
        got = TRUE;
    }
    if (transport->IncomingCount == 0) {
        KeClearEvent(&transport->IncomingEvent);
    }
    KeReleaseSpinLock(&transport->IndicationLock, old_irql);
    return got;
}

static USHORT SontNegotiatedMtu(USHORT out_mtu, USHORT in_mtu)
{
    /* We advertise a large local MTU, so the smaller of the two negotiated
     * values is the headset's receive MTU, whichever field carries it. */
    USHORT mtu = 0;
    if (out_mtu != 0 && in_mtu != 0) {
        mtu = (out_mtu < in_mtu) ? out_mtu : in_mtu;
    } else {
        mtu = (out_mtu != 0) ? out_mtu : in_mtu;
    }
    if (mtu < L2CAP_MIN_MTU) {
        mtu = SONT_L2CAP_DEFAULT_MTU;
    }
    return mtu;
}

static VOID SontFillChannelConfig(struct _BRB_L2CA_OPEN_CHANNEL *brb, PSONT_TRANSPORT transport,
                                  USHORT flush_to_ms)
{
    brb->ChannelFlags = CF_ROLE_EITHER;
    brb->ConfigOut.Flags = CFG_MTU;
    brb->ConfigOut.Mtu.Min = L2CAP_MIN_MTU;
    brb->ConfigOut.Mtu.Max = SONT_L2CAP_LOCAL_MTU;
    brb->ConfigOut.Mtu.Preferred = SONT_L2CAP_LOCAL_MTU;
    if (flush_to_ms != 0 && flush_to_ms != 0xFFFF) {
        brb->ConfigOut.Flags |= CFG_FLUSHTO;
        brb->ConfigOut.FlushTO.Min = 1;
        brb->ConfigOut.FlushTO.Max = 0xFFFF;
        brb->ConfigOut.FlushTO.Preferred = flush_to_ms;
    }
    brb->ConfigIn.Flags = CFG_MTU;
    brb->ConfigIn.Mtu.Min = L2CAP_MIN_MTU;
    brb->ConfigIn.Mtu.Max = 0xFFFF;
    brb->ConfigIn.Mtu.Preferred = SONT_L2CAP_DEFAULT_MTU;
    brb->CallbackFlags = CALLBACK_DISCONNECT;
    brb->Callback = SontChannelCallback;
    brb->CallbackContext = transport;
    brb->ReferenceObject = transport->Parent;
    brb->IncomingQueueDepth = 16;
}

NTSTATUS SontTransportAccept(PSONT_TRANSPORT transport, const SONT_INCOMING *incoming,
                             USHORT flush_to_ms, L2CAP_CHANNEL_HANDLE *channel, USHORT *mtu)
{
    struct _BRB_L2CA_OPEN_CHANNEL *brb;
    NTSTATUS status;

    if (transport == NULL || incoming == NULL || channel == NULL || !transport->HasBth) {
        return STATUS_INVALID_PARAMETER;
    }
    brb = (struct _BRB_L2CA_OPEN_CHANNEL *)transport->Bth.BthAllocateBrb(
        BRB_L2CA_OPEN_CHANNEL_RESPONSE, SONT_POOL_TAG);
    if (brb == NULL) {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    brb->BtAddress = incoming->Address;
    brb->ChannelHandle = incoming->Handle;
    brb->Response = CONNECT_RSP_RESULT_SUCCESS;   /* union with Psm: do not set Psm */
    SontFillChannelConfig(brb, transport, flush_to_ms);

    status = SontSubmitBrbTimed(transport->Parent, brb, NULL, 10000);
    if (NT_SUCCESS(status)) {
        *channel = brb->ChannelHandle;
        if (mtu != NULL) {
            *mtu = SontNegotiatedMtu(brb->OutResults.Params.Mtu, brb->InResults.Params.Mtu);
        }
    } else {
        DbgPrint("Sonatina: OPEN_CHANNEL_RESPONSE failed 0x%08X\n", status);
    }
    transport->Bth.BthFreeBrb((PBRB)brb);
    return status;
}

VOID SontTransportRefuse(PSONT_TRANSPORT transport, const SONT_INCOMING *incoming)
{
    struct _BRB_L2CA_OPEN_CHANNEL *brb;

    if (transport == NULL || incoming == NULL || !transport->HasBth) {
        return;
    }
    brb = (struct _BRB_L2CA_OPEN_CHANNEL *)transport->Bth.BthAllocateBrb(
        BRB_L2CA_OPEN_CHANNEL_RESPONSE, SONT_POOL_TAG);
    if (brb == NULL) {
        return;
    }
    brb->BtAddress = incoming->Address;
    brb->ChannelHandle = incoming->Handle;
    brb->Response = CONNECT_RSP_RESULT_NO_RESOURCES;
    brb->ReferenceObject = transport->Parent;
    (void)SontSubmitBrbTimed(transport->Parent, brb, NULL, 5000);
    transport->Bth.BthFreeBrb((PBRB)brb);
}

NTSTATUS SontTransportOpen(PSONT_TRANSPORT transport, USHORT psm, USHORT flush_to_ms,
                           PKEVENT stop_event, ULONG timeout_ms,
                           L2CAP_CHANNEL_HANDLE *channel, USHORT *mtu)
{
    struct _BRB_L2CA_OPEN_CHANNEL *brb;
    NTSTATUS status;

    if (transport == NULL || channel == NULL || !transport->HasBth) {
        return STATUS_INVALID_PARAMETER;
    }
    brb = (struct _BRB_L2CA_OPEN_CHANNEL *)transport->Bth.BthAllocateBrb(
        BRB_L2CA_OPEN_CHANNEL, SONT_POOL_TAG);
    if (brb == NULL) {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    brb->BtAddress = transport->RemoteAddress;
    brb->Psm = psm;
    SontFillChannelConfig(brb, transport, flush_to_ms);

    status = SontSubmitBrbTimed(transport->Parent, brb, stop_event, timeout_ms);
    if (NT_SUCCESS(status)) {
        *channel = brb->ChannelHandle;
        if (mtu != NULL) {
            *mtu = SontNegotiatedMtu(brb->OutResults.Params.Mtu, brb->InResults.Params.Mtu);
        }
    } else {
        DbgPrint("Sonatina: BRB_L2CA_OPEN_CHANNEL(psm=0x%04X) failed 0x%08X\n", psm, status);
    }
    transport->Bth.BthFreeBrb((PBRB)brb);
    return status;
}

NTSTATUS SontTransportSend(PSONT_TRANSPORT transport, L2CAP_CHANNEL_HANDLE channel,
                           const void *data, ULONG length)
{
    struct _BRB_L2CA_ACL_TRANSFER *brb;
    NTSTATUS status;

    if (transport == NULL || channel == NULL || data == NULL || !transport->HasBth) {
        return STATUS_INVALID_PARAMETER;
    }
    brb = (struct _BRB_L2CA_ACL_TRANSFER *)transport->Bth.BthAllocateBrb(
        BRB_L2CA_ACL_TRANSFER, SONT_POOL_TAG);
    if (brb == NULL) {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    brb->BtAddress = transport->RemoteAddress;
    brb->ChannelHandle = channel;
    brb->TransferFlags = ACL_TRANSFER_DIRECTION_OUT;
    brb->Buffer = (PVOID)data;
    brb->BufferSize = length;
    brb->BufferMDL = NULL;
    brb->Timeout = 0;

    /* Bounded so a stuck controller cannot freeze the worker forever. */
    status = SontSubmitBrbTimed(transport->Parent, brb, NULL, 2000);
    transport->Bth.BthFreeBrb((PBRB)brb);
    return status;
}

LONG SontTransportTakeLost(PSONT_TRANSPORT transport)
{
    LONG mask;
    if (transport == NULL) {
        return 0;
    }
    KeClearEvent(&transport->LinkEvent);
    mask = InterlockedExchange(&transport->LostMask, 0);
    return mask;
}

VOID SontTransportClose(PSONT_TRANSPORT transport, L2CAP_CHANNEL_HANDLE *channel)
{
    struct _BRB_L2CA_CLOSE_CHANNEL *brb;

    if (transport == NULL || channel == NULL || *channel == NULL || !transport->HasBth) {
        return;
    }
    brb = (struct _BRB_L2CA_CLOSE_CHANNEL *)transport->Bth.BthAllocateBrb(
        BRB_L2CA_CLOSE_CHANNEL, SONT_POOL_TAG);
    if (brb != NULL) {
        brb->BtAddress = transport->RemoteAddress;
        brb->ChannelHandle = *channel;
        (void)SontSubmitBrbTimed(transport->Parent, brb, NULL, 5000);
        transport->Bth.BthFreeBrb((PBRB)brb);
    }
    *channel = NULL;
}

VOID SontTransportTeardown(PSONT_TRANSPORT transport)
{
    SONT_INCOMING pending;

    if (transport == NULL || !transport->HasBth) {
        return;
    }
    SontTransportClose(transport, &transport->MediaChannel);
    SontTransportClose(transport, &transport->SignalingChannel);
    SontTransportUnregisterServer(transport);
    while (SontTransportTakeIncoming(transport, &pending)) {
        SontTransportRefuse(transport, &pending);
    }
    transport->HasBth = FALSE;
}

/* ------------------------------------------------------------------ async receive */

static NTSTATUS SontAsyncRxCompletion(PDEVICE_OBJECT DeviceObject, PIRP Irp, PVOID Context)
{
    PSONT_ASYNC_RX rx = (PSONT_ASYNC_RX)Context;
    UNREFERENCED_PARAMETER(DeviceObject);
    UNREFERENCED_PARAMETER(Irp);
    KeSetEvent(&rx->Done, IO_NO_INCREMENT, FALSE);
    return STATUS_MORE_PROCESSING_REQUIRED;  /* the IRP is reused */
}

NTSTATUS SontAsyncRxInit(PSONT_ASYNC_RX rx, PSONT_TRANSPORT transport)
{
    if (rx == NULL || transport == NULL || !transport->HasBth) {
        return STATUS_INVALID_PARAMETER;
    }
    RtlZeroMemory(rx, FIELD_OFFSET(SONT_ASYNC_RX, Buffer));
    rx->Transport = transport;
    KeInitializeEvent(&rx->Done, NotificationEvent, FALSE);
    rx->Irp = IoAllocateIrp(transport->Parent->StackSize, FALSE);
    rx->Brb = (struct _BRB_L2CA_ACL_TRANSFER *)transport->Bth.BthAllocateBrb(
        BRB_L2CA_ACL_TRANSFER, SONT_POOL_TAG);
    if (rx->Irp == NULL || rx->Brb == NULL) {
        SontAsyncRxFree(rx);
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    return STATUS_SUCCESS;
}

NTSTATUS SontAsyncRxStart(PSONT_ASYNC_RX rx, L2CAP_CHANNEL_HANDLE channel)
{
    PIO_STACK_LOCATION stack;
    PSONT_TRANSPORT transport;

    if (rx == NULL || rx->Irp == NULL || rx->Brb == NULL || channel == NULL || rx->Pending) {
        return STATUS_INVALID_PARAMETER;
    }
    transport = rx->Transport;
    transport->Bth.BthReuseBrb((PBRB)rx->Brb, BRB_L2CA_ACL_TRANSFER);
    rx->Brb->BtAddress = transport->RemoteAddress;
    rx->Brb->ChannelHandle = channel;
    rx->Brb->TransferFlags = ACL_TRANSFER_DIRECTION_IN | ACL_SHORT_TRANSFER_OK;
    rx->Brb->Buffer = rx->Buffer;
    rx->Brb->BufferSize = sizeof(rx->Buffer);
    rx->Brb->BufferMDL = NULL;
    rx->Brb->Timeout = 0;

    IoReuseIrp(rx->Irp, STATUS_SUCCESS);
    stack = IoGetNextIrpStackLocation(rx->Irp);
    stack->MajorFunction = IRP_MJ_INTERNAL_DEVICE_CONTROL;
    stack->Parameters.DeviceIoControl.IoControlCode = IOCTL_INTERNAL_BTH_SUBMIT_BRB;
    stack->Parameters.Others.Argument1 = rx->Brb;
    IoSetCompletionRoutine(rx->Irp, SontAsyncRxCompletion, rx, TRUE, TRUE, TRUE);

    KeClearEvent(&rx->Done);
    rx->Pending = TRUE;
    rx->Length = 0;
    (void)IoCallDriver(transport->Parent, rx->Irp);
    return STATUS_PENDING;
}

VOID SontAsyncRxComplete(PSONT_ASYNC_RX rx)
{
    if (rx == NULL || !rx->Pending) {
        return;
    }
    KeWaitForSingleObject(&rx->Done, Executive, KernelMode, FALSE, NULL);
    rx->Pending = FALSE;
    rx->Status = rx->Irp->IoStatus.Status;
    rx->Length = 0;
    if (NT_SUCCESS(rx->Status)) {
        rx->Length = (rx->Brb->BufferSize <= sizeof(rx->Buffer)) ? rx->Brb->BufferSize
                                                                 : (ULONG)sizeof(rx->Buffer);
    }
}

VOID SontAsyncRxCancel(PSONT_ASYNC_RX rx)
{
    if (rx == NULL || !rx->Pending) {
        return;
    }
    (void)IoCancelIrp(rx->Irp);
    SontAsyncRxComplete(rx);
}

VOID SontAsyncRxFree(PSONT_ASYNC_RX rx)
{
    if (rx == NULL) {
        return;
    }
    SontAsyncRxCancel(rx);
    if (rx->Brb != NULL && rx->Transport != NULL) {
        rx->Transport->Bth.BthFreeBrb((PBRB)rx->Brb);
    }
    if (rx->Irp != NULL) {
        IoFreeIrp(rx->Irp);
    }
    rx->Brb = NULL;
    rx->Irp = NULL;
}

/* ------------------------------------------------------------------ async transmit queue */

static NTSTATUS SontTxCompletion(PDEVICE_OBJECT DeviceObject, PIRP Irp, PVOID Context)
{
    PSONT_TX_SLOT slot = (PSONT_TX_SLOT)Context;
    PSONT_TX_QUEUE queue = slot->Queue;
    UNREFERENCED_PARAMETER(DeviceObject);

    slot->Status = Irp->IoStatus.Status;
    slot->DoneQpc = KeQueryPerformanceCounter(NULL).QuadPart;
    InterlockedExchangeAdd(&queue->InFlightAudioUs, -(LONG)slot->AudioUs);
    InterlockedExchange(&slot->Completed, 1);
    KeSetEvent(&queue->Done, IO_NO_INCREMENT, FALSE);
    InterlockedExchange(&slot->Busy, 0);
    /* Last access to the queue: once InFlight reaches 0 the owner may free it. */
    InterlockedDecrement(&queue->InFlight);
    return STATUS_MORE_PROCESSING_REQUIRED;  /* IRP is reused */
}

NTSTATUS SontTxQueueInit(PSONT_TX_QUEUE queue, PSONT_TRANSPORT transport)
{
    LARGE_INTEGER freq;
    ULONG i;

    if (queue == NULL || transport == NULL || !transport->HasBth) {
        return STATUS_INVALID_PARAMETER;
    }
    RtlZeroMemory(queue, sizeof(*queue));
    queue->Transport = transport;
    KeInitializeEvent(&queue->Done, NotificationEvent, FALSE);
    (void)KeQueryPerformanceCounter(&freq);
    queue->QpcFrequency = (freq.QuadPart > 0) ? freq.QuadPart : 10000000LL;
    for (i = 0; i < SONT_TX_SLOTS; i++) {
        PSONT_TX_SLOT slot = &queue->Slots[i];
        slot->Queue = queue;
        slot->Irp = IoAllocateIrp(transport->Parent->StackSize, FALSE);
        slot->Brb = (struct _BRB_L2CA_ACL_TRANSFER *)transport->Bth.BthAllocateBrb(
            BRB_L2CA_ACL_TRANSFER, SONT_POOL_TAG);
        if (slot->Irp == NULL || slot->Brb == NULL) {
            SontTxQueueFree(queue);
            return STATUS_INSUFFICIENT_RESOURCES;
        }
    }
    return STATUS_SUCCESS;
}

PSONT_TX_SLOT SontTxQueueAcquire(PSONT_TX_QUEUE queue)
{
    ULONG i;

    if (queue == NULL || queue->Transport == NULL) {
        return NULL;
    }
    for (i = 0; i < SONT_TX_SLOTS; i++) {
        PSONT_TX_SLOT slot = &queue->Slots[i];
        if (slot->Irp != NULL && slot->Busy == 0 && slot->Completed == 0) {
            return slot;
        }
    }
    return NULL;
}

VOID SontTxQueueSubmit(PSONT_TX_SLOT slot, L2CAP_CHANNEL_HANDLE channel,
                       ULONG length, ULONG audio_us)
{
    PSONT_TX_QUEUE queue = slot->Queue;
    PSONT_TRANSPORT transport = queue->Transport;
    PIO_STACK_LOCATION stack;

    transport->Bth.BthReuseBrb((PBRB)slot->Brb, BRB_L2CA_ACL_TRANSFER);
    slot->Brb->BtAddress = transport->RemoteAddress;
    slot->Brb->ChannelHandle = channel;
    slot->Brb->TransferFlags = ACL_TRANSFER_DIRECTION_OUT;
    slot->Brb->Buffer = slot->Buffer;
    slot->Brb->BufferSize = (length <= SONT_TX_SLOT_BYTES) ? length : SONT_TX_SLOT_BYTES;
    slot->Brb->BufferMDL = NULL;
    slot->Brb->Timeout = 0;

    IoReuseIrp(slot->Irp, STATUS_SUCCESS);
    stack = IoGetNextIrpStackLocation(slot->Irp);
    stack->MajorFunction = IRP_MJ_INTERNAL_DEVICE_CONTROL;
    stack->Parameters.DeviceIoControl.IoControlCode = IOCTL_INTERNAL_BTH_SUBMIT_BRB;
    stack->Parameters.Others.Argument1 = slot->Brb;
    IoSetCompletionRoutine(slot->Irp, SontTxCompletion, slot, TRUE, TRUE, TRUE);

    slot->AudioUs = audio_us;
    slot->Status = STATUS_PENDING;
    slot->SubmitQpc = KeQueryPerformanceCounter(NULL).QuadPart;
    InterlockedIncrement(&queue->InFlight);
    InterlockedExchangeAdd(&queue->InFlightAudioUs, (LONG)audio_us);
    InterlockedExchange(&slot->Busy, 1);
    (void)IoCallDriver(transport->Parent, slot->Irp);  /* completion routine always runs */
}

BOOLEAN SontTxQueueReap(PSONT_TX_QUEUE queue, NTSTATUS *status, ULONG *latency_us)
{
    ULONG i;

    if (queue == NULL) {
        return FALSE;
    }
    for (i = 0; i < SONT_TX_SLOTS; i++) {
        PSONT_TX_SLOT slot = &queue->Slots[i];
        if (slot->Completed != 0 && slot->Busy == 0) {
            LONGLONG ticks = slot->DoneQpc - slot->SubmitQpc;
            if (status != NULL) {
                *status = slot->Status;
            }
            if (latency_us != NULL) {
                *latency_us = (ticks > 0) ? (ULONG)((ticks * 1000000LL) / queue->QpcFrequency) : 0u;
            }
            InterlockedExchange(&slot->Completed, 0);
            return TRUE;
        }
    }
    KeClearEvent(&queue->Done);
    return FALSE;
}

VOID SontTxQueueDrain(PSONT_TX_QUEUE queue)
{
    ULONG i;
    LARGE_INTEGER tick;

    if (queue == NULL || queue->Transport == NULL) {
        return;
    }
    for (i = 0; i < SONT_TX_SLOTS; i++) {
        if (queue->Slots[i].Busy != 0 && queue->Slots[i].Irp != NULL) {
            (void)IoCancelIrp(queue->Slots[i].Irp);
        }
    }
    tick.QuadPart = -10LL * 10000LL;  /* 10 ms */
    while (InterlockedCompareExchange(&queue->InFlight, 0, 0) > 0) {
        (void)KeWaitForSingleObject(&queue->Done, Executive, KernelMode, FALSE, &tick);
        KeClearEvent(&queue->Done);
    }
    for (i = 0; i < SONT_TX_SLOTS; i++) {
        InterlockedExchange(&queue->Slots[i].Completed, 0);
    }
    queue->InFlightAudioUs = 0;
}

VOID SontTxQueueFree(PSONT_TX_QUEUE queue)
{
    ULONG i;

    if (queue == NULL || queue->Transport == NULL) {
        return;
    }
    SontTxQueueDrain(queue);
    for (i = 0; i < SONT_TX_SLOTS; i++) {
        PSONT_TX_SLOT slot = &queue->Slots[i];
        if (slot->Brb != NULL) {
            queue->Transport->Bth.BthFreeBrb((PBRB)slot->Brb);
            slot->Brb = NULL;
        }
        if (slot->Irp != NULL) {
            IoFreeIrp(slot->Irp);
            slot->Irp = NULL;
        }
    }
    queue->Transport = NULL;
}
