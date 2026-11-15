/* Minimal PortCls Topology miniport for Sonatina (headphones endpoint bridge + jack props). */
#ifndef SONATINA_MINIPORT_TOPOLOGY_H
#define SONATINA_MINIPORT_TOPOLOGY_H

#include <ntddk.h>
#pragma warning(push)
#pragma warning(disable: 4115 4127 4201 4595)
#include <portcls.h>
#include <stdunk.h>
#pragma warning(pop)

#include "NewDelete.h"

class CMiniportTopologySonatina :
    public IMiniportTopology,
    public CUnknown
{
public:
    DECLARE_STD_UNKNOWN();
    CMiniportTopologySonatina(_In_opt_ PUNKNOWN UnknownOuter)
        : CUnknown(UnknownOuter)
    {
    }
    ~CMiniportTopologySonatina();

    IMP_IMiniportTopology;
};

typedef CMiniportTopologySonatina *PCMiniportTopologySonatina;

_IRQL_requires_max_(PASSIVE_LEVEL)
NTSTATUS
SontCreateTopologyMiniport(
    _Outptr_ PUNKNOWN *Unknown
);

#endif /* SONATINA_MINIPORT_TOPOLOGY_H */
