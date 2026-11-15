/*++

Copyright (c) Microsoft Corporation All Rights Reserved

Module Name:

    simple.h

Abstract:

    Pin and node identifiers for Sonatina 2-pin WaveRT + Topology subdevices.
    Adapted from Microsoft Windows-driver-samples/audio/sysvad/EndpointsCommon/simple.h (MS-PL).

--*/

#ifndef _SYSVAD_SIMPLE_H_
#define _SYSVAD_SIMPLE_H_

// Wave pins (host render sink -> bridge source; no offload / no loopback in D2).
enum
{
    KSPIN_WAVE_RENDER3_SINK_SYSTEM = 0,
    KSPIN_WAVE_RENDER3_SOURCE
};

// Topology pins (wave bridge input -> physical endpoint output).
enum
{
    KSPIN_TOPO_WAVEOUT_SOURCE = 0,
    KSPIN_TOPO_LINEOUT_DEST
};

#endif // _SYSVAD_SIMPLE_H_
