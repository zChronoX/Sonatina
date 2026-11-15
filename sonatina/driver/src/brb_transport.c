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

/* Submit one BRB synchronously. The caller frees the BRB afterwards. */
static NTSTATUS SontSubmitBrb(PDEVICE_OBJECT parent, PVOID brb)
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
        KeWaitForSingleObject(&event, Executive, KernelMode, FALSE, NULL);
    }
    status = irp->IoStatus.Status;
    IoFreeIrp(irp);
    return status;
}

static void SontL2capCallback(PVOID context, INDICATION_CODE indication,
                              PINDICATION_PARAMETERS parameters)
{
    PSONT_TRANSPORT transport = (PSONT_TRANSPORT)context;

    UNREFERENCED_PARAMETER(parameters);

    switch (indication) {
    case IndicationRemoteConnect:
        /* Incoming request: accepted only after SetConfiguration in D3. */
        DbgPrint("Sonatina: remote L2CAP connect indication\n");
        break;
    case IndicationRemoteDisconnect:
        DbgPrint("Sonatina: remote L2CAP disconnect indication\n");
        if (transport != NULL) {
            transport->SignalingChannel = NULL;
            transport->MediaChannel = NULL;
        }
        break;
    default:
        break;
    }
}

NTSTATUS SontTransportInitialize(PSONT_TRANSPORT transport, PDEVICE_OBJECT parent)
{
    NTSTATUS status;

    if (transport == NULL || parent == NULL) {
        return STATUS_INVALID_PARAMETER;
    }
    RtlZeroMemory(transport, sizeof(*transport));
    transport->Parent = parent;

    status = SontQueryBthInterface(parent, &transport->Bth);
    if (!NT_SUCCESS(status)) {
        DbgPrint("Sonatina: QUERY_INTERFACE(BTHDDI) failed 0x%08X\n", status);
        return status;
    }
    if (transport->Bth.BthAllocateBrb == NULL ||
        transport->Bth.BthFreeBrb == NULL) {
        return STATUS_UNSUCCESSFUL;
    }
    transport->HasBth = TRUE;

    status = SontQueryRemoteAddress(parent, &transport->RemoteAddress);
    if (!NT_SUCCESS(status)) {
        DbgPrint("Sonatina: BTENUM GET_DEVINFO failed 0x%08X\n", status);
        return status;
    }
    DbgPrint("Sonatina: remote address %llX\n", (unsigned long long)transport->RemoteAddress);
    return STATUS_SUCCESS;
}

NTSTATUS SontTransportOpen(PSONT_TRANSPORT transport, USHORT psm,
                           PL2CAP_CHANNEL_HANDLE channel)
{
    PBRB_L2CA_OPEN_CHANNEL brb;
    NTSTATUS status;

    if (transport == NULL || channel == NULL || !transport->HasBth) {
        return STATUS_INVALID_PARAMETER;
    }
    brb = (PBRB_L2CA_OPEN_CHANNEL)transport->Bth.BthAllocateBrb(
        BRB_L2CA_OPEN_CHANNEL, SONT_POOL_TAG);
    if (brb == NULL) {
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    brb->BtAddress = transport->RemoteAddress;
    brb->Psm = psm;
    brb->ChannelFlags = CF_ROLE_EITHER;
    brb->ConfigOut.Flags = CFG_MTU | CFG_FLUSHTO;
    brb->ConfigOut.Mtu.Min = L2CAP_MIN_MTU;
    brb->ConfigOut.Mtu.Max = 0xFFFF;
    brb->ConfigOut.Mtu.Preferred = L2CAP_DEFAULT_MTU;
    brb->ConfigOut.FlushTO.Min = 1;
    brb->ConfigOut.FlushTO.Max = 0xFFFF;
    brb->ConfigOut.FlushTO.Preferred = 0xFFFF;
    brb->ConfigIn.Flags = CFG_MTU;
    brb->ConfigIn.Mtu.Min = L2CAP_MIN_MTU;
    brb->ConfigIn.Mtu.Max = 0xFFFF;
    brb->ConfigIn.Mtu.Preferred = L2CAP_DEFAULT_MTU;
    brb->CallbackFlags = CALLBACK_DISCONNECT;
    brb->Callback = SontL2capCallback;
    brb->CallbackContext = transport;
    brb->ReferenceObject = transport->Parent;
    brb->IncomingQueueDepth = 10;

    status = SontSubmitBrb(transport->Parent, brb);
    if (NT_SUCCESS(status)) {
        *channel = brb->ChannelHandle;
    } else {
        DbgPrint("Sonatina: BRB_L2CA_OPEN_CHANNEL(psm=0x%04X) failed 0x%08X\n", psm, status);
    }
    transport->Bth.BthFreeBrb((PBRB)brb);
    return status;
}

NTSTATUS SontTransportSend(PSONT_TRANSPORT transport, L2CAP_CHANNEL_HANDLE channel,
                           const void *data, ULONG length)
{
    PBRB_L2CA_ACL_TRANSFER brb;
    NTSTATUS status;

    if (transport == NULL || channel == NULL || data == NULL || !transport->HasBth) {
        return STATUS_INVALID_PARAMETER;
    }
    brb = (PBRB_L2CA_ACL_TRANSFER)transport->Bth.BthAllocateBrb(
        BRB_L2CA_ACL_TRANSFER, SONT_POOL_TAG);
    if (brb == NULL) {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    brb->ChannelHandle = channel;
    brb->TransferFlags = ACL_TRANSFER_DIRECTION_OUT;
    brb->Buffer = (PVOID)data;
    brb->BufferSize = length;
    brb->BufferMDL = NULL;
    brb->Timeout = 0;

    status = SontSubmitBrb(transport->Parent, brb);
    transport->Bth.BthFreeBrb((PBRB)brb);
    return status;
}

VOID SontTransportClose(PSONT_TRANSPORT transport, PL2CAP_CHANNEL_HANDLE channel)
{
    PBRB_L2CA_CLOSE_CHANNEL brb;

    if (transport == NULL || channel == NULL || *channel == NULL || !transport->HasBth) {
        return;
    }
    brb = (PBRB_L2CA_CLOSE_CHANNEL)transport->Bth.BthAllocateBrb(
        BRB_L2CA_CLOSE_CHANNEL, SONT_POOL_TAG);
    if (brb != NULL) {
        brb->ChannelHandle = *channel;
        (void)SontSubmitBrb(transport->Parent, brb);
        transport->Bth.BthFreeBrb((PBRB)brb);
    }
    *channel = NULL;
}

VOID SontTransportTeardown(PSONT_TRANSPORT transport)
{
    if (transport == NULL) {
        return;
    }
    SontTransportClose(transport, &transport->MediaChannel);
    SontTransportClose(transport, &transport->SignalingChannel);
    transport->HasBth = FALSE;
}
