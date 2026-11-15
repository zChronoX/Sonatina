/* Minimal PortCls Topology miniport for Sonatina (D2: bridge + jack properties).
 * Pin/node indices and jack property structure follow Microsoft SysVAD (MS-PL).
 */
#include "miniport_topology.h"
#include "simple.h"

#define SONT_TOPOLOGY_POOLTAG 'anOS'

static NTSTATUS
PropertyHandler_SontTopoFilter(
    _In_ PPCPROPERTY_REQUEST PropertyRequest
);

static KSDATARANGE SontTopoPinDataRangesBridge[] =
{
    {
        sizeof(KSDATARANGE),
        0,
        0,
        0,
        STATICGUIDOF(KSDATAFORMAT_TYPE_AUDIO),
        STATICGUIDOF(KSDATAFORMAT_SUBTYPE_ANALOG),
        STATICGUIDOF(KSDATAFORMAT_SPECIFIER_NONE)
    }
};

static PKSDATARANGE SontTopoPinDataRangePointersBridge[] =
{
    &SontTopoPinDataRangesBridge[0]
};

static PCPIN_DESCRIPTOR SontTopoMiniportPins[] =
{
    /* KSPIN_TOPO_WAVEOUT_SOURCE (bridge input from WaveRT filter) */
    {
        0,
        0,
        0,
        NULL,
        {
            0,
            NULL,
            0,
            NULL,
            ARRAYSIZE(SontTopoPinDataRangePointersBridge),
            SontTopoPinDataRangePointersBridge,
            KSPIN_DATAFLOW_IN,
            KSPIN_COMMUNICATION_NONE,
            &KSCATEGORY_AUDIO,
            NULL,
            0
        }
    },
    /* KSPIN_TOPO_LINEOUT_DEST (physical Bluetooth headphones output) */
    {
        0,
        0,
        0,
        NULL,
        {
            0,
            NULL,
            0,
            NULL,
            ARRAYSIZE(SontTopoPinDataRangePointersBridge),
            SontTopoPinDataRangePointersBridge,
            KSPIN_DATAFLOW_OUT,
            KSPIN_COMMUNICATION_NONE,
            &KSNODETYPE_HEADPHONES,
            NULL,
            0
        }
    }
};

static KSJACK_DESCRIPTION SontHeadphonesJackDesc =
{
    KSAUDIO_SPEAKER_STEREO,
    0x00000000,
    eConnTypeUnknown,
    eGeoLocFront,
    eGenLocPrimaryBox,
    ePortConnIntegratedDevice,
    TRUE
};

static PKSJACK_DESCRIPTION SontTopoJackDescriptions[] =
{
    NULL,
    &SontHeadphonesJackDesc
};

static PCCONNECTION_DESCRIPTOR SontTopoMiniportConnections[] =
{
    {
        PCFILTER_NODE,
        KSPIN_TOPO_WAVEOUT_SOURCE,
        PCFILTER_NODE,
        KSPIN_TOPO_LINEOUT_DEST
    }
};

static PCPROPERTY_ITEM SontPropertiesTopoFilter[] =
{
    {
        &KSPROPSETID_Jack,
        KSPROPERTY_JACK_DESCRIPTION,
        KSPROPERTY_TYPE_GET | KSPROPERTY_TYPE_BASICSUPPORT,
        PropertyHandler_SontTopoFilter
    },
    {
        &KSPROPSETID_Jack,
        KSPROPERTY_JACK_DESCRIPTION2,
        KSPROPERTY_TYPE_GET | KSPROPERTY_TYPE_BASICSUPPORT,
        PropertyHandler_SontTopoFilter
    }
};

DEFINE_PCAUTOMATION_TABLE_PROP(
    SontAutomationTopoFilter,
    SontPropertiesTopoFilter
);

static PCFILTER_DESCRIPTOR SontTopoMiniportFilterDescriptor =
{
    0,
    &SontAutomationTopoFilter,
    sizeof(PCPIN_DESCRIPTOR),
    ARRAYSIZE(SontTopoMiniportPins),
    SontTopoMiniportPins,
    sizeof(PCNODE_DESCRIPTOR),
    0,
    NULL,
    ARRAYSIZE(SontTopoMiniportConnections),
    SontTopoMiniportConnections,
    0,
    NULL
};

#pragma code_seg("PAGE")
static NTSTATUS
PropertyHandler_BasicSupportGetOnly(
    _In_ PPCPROPERTY_REQUEST PropertyRequest
)
{
    PAGED_CODE();

    if (PropertyRequest->ValueSize >= sizeof(KSPROPERTY_DESCRIPTION))
    {
        PKSPROPERTY_DESCRIPTION desc = (PKSPROPERTY_DESCRIPTION)PropertyRequest->Value;
        desc->AccessFlags = KSPROPERTY_TYPE_BASICSUPPORT | KSPROPERTY_TYPE_GET;
        desc->DescriptionSize = sizeof(KSPROPERTY_DESCRIPTION);
        desc->PropTypeSet.Set = GUID_NULL;
        desc->PropTypeSet.Id = 0;
        desc->PropTypeSet.Flags = 0;
        desc->MembersListCount = 0;
        desc->Reserved = 0;
        PropertyRequest->ValueSize = sizeof(KSPROPERTY_DESCRIPTION);
        return STATUS_SUCCESS;
    }

    if (PropertyRequest->ValueSize >= sizeof(ULONG))
    {
        *(PULONG)PropertyRequest->Value = KSPROPERTY_TYPE_BASICSUPPORT | KSPROPERTY_TYPE_GET;
        PropertyRequest->ValueSize = sizeof(ULONG);
        return STATUS_SUCCESS;
    }

    PropertyRequest->ValueSize = 0;
    return STATUS_BUFFER_TOO_SMALL;
}

#pragma code_seg("PAGE")
static NTSTATUS
PropertyHandler_SontTopoFilter(
    _In_ PPCPROPERTY_REQUEST PropertyRequest
)
{
    PAGED_CODE();

    if (!PropertyRequest || !PropertyRequest->PropertyItem || !PropertyRequest->PropertyItem->Set)
    {
        return STATUS_INVALID_PARAMETER;
    }

    if (!IsEqualGUIDAligned(*PropertyRequest->PropertyItem->Set, KSPROPSETID_Jack))
    {
        return STATUS_INVALID_DEVICE_REQUEST;
    }

    if (PropertyRequest->InstanceSize < sizeof(ULONG) || !PropertyRequest->Instance)
    {
        return STATUS_INVALID_PARAMETER;
    }

    ULONG pinId = *(PULONG)PropertyRequest->Instance;
    if (pinId >= ARRAYSIZE(SontTopoJackDescriptions) || SontTopoJackDescriptions[pinId] == NULL)
    {
        return STATUS_INVALID_PARAMETER;
    }

    if (PropertyRequest->Verb & KSPROPERTY_TYPE_BASICSUPPORT)
    {
        return PropertyHandler_BasicSupportGetOnly(PropertyRequest);
    }

    if (!(PropertyRequest->Verb & KSPROPERTY_TYPE_GET))
    {
        return STATUS_INVALID_DEVICE_REQUEST;
    }

    ULONG id = PropertyRequest->PropertyItem->Id;
    if (id == KSPROPERTY_JACK_DESCRIPTION)
    {
        ULONG needed = sizeof(KSMULTIPLE_ITEM) + sizeof(KSJACK_DESCRIPTION);
        if (PropertyRequest->ValueSize == 0)
        {
            PropertyRequest->ValueSize = needed;
            return STATUS_BUFFER_OVERFLOW;
        }
        if (PropertyRequest->ValueSize < needed)
        {
            return STATUS_BUFFER_TOO_SMALL;
        }
        if (!PropertyRequest->Value)
        {
            return STATUS_INVALID_PARAMETER;
        }

        PKSMULTIPLE_ITEM mi = (PKSMULTIPLE_ITEM)PropertyRequest->Value;
        PKSJACK_DESCRIPTION desc = (PKSJACK_DESCRIPTION)(mi + 1);
        mi->Size = needed;
        mi->Count = 1;
        RtlCopyMemory(desc, SontTopoJackDescriptions[pinId], sizeof(KSJACK_DESCRIPTION));
        PropertyRequest->ValueSize = needed;
        return STATUS_SUCCESS;
    }

    if (id == KSPROPERTY_JACK_DESCRIPTION2)
    {
        ULONG needed = sizeof(KSMULTIPLE_ITEM) + sizeof(KSJACK_DESCRIPTION2);
        if (PropertyRequest->ValueSize == 0)
        {
            PropertyRequest->ValueSize = needed;
            return STATUS_BUFFER_OVERFLOW;
        }
        if (PropertyRequest->ValueSize < needed)
        {
            return STATUS_BUFFER_TOO_SMALL;
        }
        if (!PropertyRequest->Value)
        {
            return STATUS_INVALID_PARAMETER;
        }

        PKSMULTIPLE_ITEM mi = (PKSMULTIPLE_ITEM)PropertyRequest->Value;
        PKSJACK_DESCRIPTION2 desc2 = (PKSJACK_DESCRIPTION2)(mi + 1);
        mi->Size = needed;
        mi->Count = 1;
        RtlZeroMemory(desc2, sizeof(KSJACK_DESCRIPTION2));
        desc2->DeviceStateInfo = 0;
        desc2->JackCapabilities = 0;
        PropertyRequest->ValueSize = needed;
        return STATUS_SUCCESS;
    }

    return STATUS_INVALID_DEVICE_REQUEST;
}

#pragma code_seg("PAGE")
CMiniportTopologySonatina::~CMiniportTopologySonatina()
{
    PAGED_CODE();
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportTopologySonatina::NonDelegatingQueryInterface(
    _In_ REFIID Interface,
    _COM_Outptr_ PVOID *Object
)
{
    PAGED_CODE();

    if (!Object)
    {
        return STATUS_INVALID_PARAMETER;
    }

    if (IsEqualGUIDAligned(Interface, IID_IUnknown))
    {
        *Object = PVOID(PUNKNOWN(this));
    }
    else if (IsEqualGUIDAligned(Interface, IID_IMiniport))
    {
        *Object = PVOID(PMINIPORT(this));
    }
    else if (IsEqualGUIDAligned(Interface, IID_IMiniportTopology))
    {
        *Object = PVOID(PMINIPORTTOPOLOGY(this));
    }
    else
    {
        *Object = NULL;
        return STATUS_INVALID_PARAMETER;
    }

    PUNKNOWN(*Object)->AddRef();
    return STATUS_SUCCESS;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportTopologySonatina::GetDescription(
    _Out_ PPCFILTER_DESCRIPTOR *Description
)
{
    PAGED_CODE();

    if (!Description)
    {
        return STATUS_INVALID_PARAMETER;
    }

    *Description = &SontTopoMiniportFilterDescriptor;
    return STATUS_SUCCESS;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportTopologySonatina::DataRangeIntersection(
    _In_ ULONG PinId,
    _In_ PKSDATARANGE ClientDataRange,
    _In_ PKSDATARANGE MyDataRange,
    _In_ ULONG OutputBufferLength,
    _Out_writes_bytes_to_opt_(OutputBufferLength, *ResultantFormatLength) PVOID ResultantFormat,
    _Out_ PULONG ResultantFormatLength
)
{
    PAGED_CODE();

    UNREFERENCED_PARAMETER(PinId);
    UNREFERENCED_PARAMETER(ClientDataRange);
    UNREFERENCED_PARAMETER(MyDataRange);
    UNREFERENCED_PARAMETER(OutputBufferLength);
    UNREFERENCED_PARAMETER(ResultantFormat);

    if (ResultantFormatLength)
    {
        *ResultantFormatLength = 0;
    }

    return STATUS_NOT_IMPLEMENTED;
}

#pragma code_seg("PAGE")
STDMETHODIMP_(NTSTATUS)
CMiniportTopologySonatina::Init(
    _In_ PUNKNOWN UnknownAdapter,
    _In_ PRESOURCELIST ResourceList,
    _In_ PPORTTOPOLOGY Port
)
{
    PAGED_CODE();

    UNREFERENCED_PARAMETER(UnknownAdapter);
    UNREFERENCED_PARAMETER(ResourceList);

    if (!Port)
    {
        return STATUS_INVALID_PARAMETER;
    }

    return STATUS_SUCCESS;
}

#pragma code_seg("PAGE")
NTSTATUS
SontCreateTopologyMiniport(
    _Outptr_ PUNKNOWN *Unknown
)
{
    PAGED_CODE();

    if (!Unknown)
    {
        return STATUS_INVALID_PARAMETER;
    }

    *Unknown = NULL;
    CMiniportTopologySonatina *obj =
        new (POOL_FLAG_NON_PAGED, SONT_TOPOLOGY_POOLTAG) CMiniportTopologySonatina(NULL);
    if (!obj)
    {
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    obj->AddRef();
    *Unknown = PUNKNOWN(obj);
    return STATUS_SUCCESS;
}
