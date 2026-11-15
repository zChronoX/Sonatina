/* SonatinaAudio.sys - WDM/PortCls function driver for the Bluetooth AudioSink
 * devnode. This file is the scaffold: it initialises the BTHDDI transport and
 * the private control interface. WaveRT/topology subdevices and the streaming
 * thread are added in D2-D4 (see DESIGN.md).
 */
#include <ntifs.h>
#pragma warning(push)
#pragma warning(disable: 4115)
#include <portcls.h>
#pragma warning(pop)

#include "brb_transport.h"
#include <initguid.h>
#include "public.h"
#include "device.h"

#define SONT_POOL_TAG 'anOS'

static NTSTATUS SontAddDevice(PDRIVER_OBJECT driver, PDEVICE_OBJECT pdo);
static NTSTATUS SontStartDevice(PDEVICE_OBJECT device, PIRP irp, PRESOURCELIST resources);
static NTSTATUS SontDeviceControl(PDEVICE_OBJECT device, PIRP irp);

NTSTATUS DriverEntry(PDRIVER_OBJECT driver, PUNICODE_STRING registry_path)
{
    NTSTATUS status;

    status = PcInitializeAdapterDriver(driver, registry_path, SontAddDevice);
    if (!NT_SUCCESS(status)) {
        return status;
    }
    /* Private control interface; everything else falls through to PortCls. */
    driver->MajorFunction[IRP_MJ_DEVICE_CONTROL] = SontDeviceControl;
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

    UNREFERENCED_PARAMETER(irp);
    UNREFERENCED_PARAMETER(resources);

    RtlZeroMemory(ext, sizeof(*ext));
    KeInitializeSpinLock(&ext->Lock);
    status = PcGetPhysicalDeviceObject(device, &ext->PhysicalDeviceObject);
    if (!NT_SUCCESS(status)) {
        DbgPrint("Sonatina: PcGetPhysicalDeviceObject failed 0x%08X\n", status);
        return status;
    }
    ext->LowerDeviceObject = IoGetLowerDeviceObject(device);
    ext->Profile.Size = sizeof(SONATINA_PROFILE);
    ext->Profile.Codec = SonatinaCodecSbc;
    ext->Profile.SbcSampling = 48000;
    ext->Profile.SbcChannelMode = 3;
    ext->Profile.SbcBlocks = 16;
    ext->Profile.SbcSubbands = 8;
    ext->Profile.SbcAllocation = 1;
    ext->Profile.SbcMinBitpool = 2;
    ext->Profile.SbcMaxBitpool = 53;
    ext->State.Size = sizeof(SONATINA_STATE);

    status = IoRegisterDeviceInterface(ext->PhysicalDeviceObject,
                                       &GUID_DEVINTERFACE_SONATINA_CONTROL,
                                       NULL, &ext->ControlInterfaceName);
    if (NT_SUCCESS(status)) {
        ext->ControlInterfaceRegistered = TRUE;
        (void)IoSetDeviceInterfaceState(&ext->ControlInterfaceName, TRUE);
    }

    target = (ext->LowerDeviceObject != NULL) ? ext->LowerDeviceObject
                                              : ext->PhysicalDeviceObject;
    status = SontTransportInitialize(&ext->Transport, target);
    ext->TransportReady = NT_SUCCESS(status);
    if (!ext->TransportReady) {
        DbgPrint("Sonatina: transport not ready (0x%08X); control interface only\n",
                 status);
    }

    /* TODO(D2): PcRegisterSubdevice for the WaveRT and topology miniports. */
    /* TODO(D3): open SONT_L2CAP_PSM_AVDTP and run the signalling state machine. */

    /* PortCls completes IRP_MN_START_DEVICE after StartDevice returns. */
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
        /* TODO(D5): validate and apply; currently stores the profile. */
        KeAcquireSpinLock(&ext->Lock, &old_irql);
        RtlCopyMemory(&ext->Profile, buffer, sizeof(SONATINA_PROFILE));
        ext->Profile.Size = sizeof(SONATINA_PROFILE);
        KeReleaseSpinLock(&ext->Lock, old_irql);
        irp->IoStatus.Information = 0;
        break;

    case IOCTL_SONATINA_GET_STATS:
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

    case IOCTL_SONATINA_RECONNECT:
        /* TODO(D5): tear down and reopen the Bluetooth audio channels. */
        status = STATUS_NOT_IMPLEMENTED;
        break;

    default:
        return PcDispatchIrp(device, irp);
    }

    irp->IoStatus.Status = status;
    IoCompleteRequest(irp, IO_NO_INCREMENT);
    return status;
}
