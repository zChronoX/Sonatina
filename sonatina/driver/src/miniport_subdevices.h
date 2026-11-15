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

_IRQL_requires_max_(PASSIVE_LEVEL)
NTSTATUS
SontRegisterAudioSubdevices(
    _In_ PDEVICE_OBJECT DeviceObject,
    _In_ PIRP Irp,
    _In_opt_ PRESOURCELIST ResourceList
);

#ifdef __cplusplus
}
#endif

#endif /* SONATINA_MINIPORT_SUBDEVICES_H */
