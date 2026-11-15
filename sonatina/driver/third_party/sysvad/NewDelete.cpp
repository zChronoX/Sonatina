/*****************************************************************************
* NewDelete.cpp - CPP placement new and delete operators implementation
*****************************************************************************
* Copyright (c) Microsoft Corporation All Rights Reserved
*
* Module Name:
*
*   NewDelete.cpp
*
* Abstract:
*
*   Definition of placement new and delete operators.
*   Adapted minimally for Sonatina from Microsoft Windows-driver-samples (MS-PL).
*
*/

#ifdef _NEW_DELETE_OPERATORS_
extern "C" {
#include <wdm.h>
}

#include "NewDelete.h"

#pragma code_seg()

PVOID operator new
(
    size_t      iSize,
    POOL_FLAGS  poolFlags,
    ULONG       tag
)
{
    return ExAllocatePool2(poolFlags, iSize, tag);
}

PVOID operator new
(
    size_t      iSize,
    POOL_FLAGS  poolFlags
)
{
    return ExAllocatePool2(poolFlags, iSize, SONT_SYSVAD_POOLTAG);
}

void __cdecl operator delete
(
    PVOID pVoid,
    ULONG tag
)
{
    if (pVoid)
    {
        ExFreePoolWithTag(pVoid, tag);
    }
}

void __cdecl operator delete
(
    _Pre_maybenull_ __drv_freesMem(Mem) PVOID pVoid,
    _In_ size_t cbSize
)
{
    UNREFERENCED_PARAMETER(cbSize);

    if (pVoid)
    {
        ExFreePoolWithTag(pVoid, SONT_SYSVAD_POOLTAG);
    }
}

#ifdef __cpp_aligned_new
void __cdecl operator delete
(
    _Pre_maybenull_ __drv_freesMem(Mem) PVOID pVoid,
    _In_ size_t cbSize,
    _In_ std::align_val_t cbAlign
)
{
    UNREFERENCED_PARAMETER(cbSize);
    UNREFERENCED_PARAMETER(cbAlign);

    if (pVoid)
    {
        ExFreePoolWithTag(pVoid, SONT_SYSVAD_POOLTAG);
    }
}
#endif // __cpp_aligned_new

void __cdecl operator delete
(
    PVOID pVoid
)
{
    if (pVoid)
    {
        ExFreePoolWithTag(pVoid, SONT_SYSVAD_POOLTAG);
    }
}

void __cdecl operator delete[]
(
    _Pre_maybenull_ __drv_freesMem(Mem) PVOID pVoid,
    _In_ size_t cbSize
)
{
    UNREFERENCED_PARAMETER(cbSize);

    if (pVoid)
    {
        ExFreePoolWithTag(pVoid, SONT_SYSVAD_POOLTAG);
    }
}

void __cdecl operator delete[]
(
    _Pre_maybenull_ __drv_freesMem(Mem) PVOID pVoid
)
{
    if (pVoid)
    {
        ExFreePoolWithTag(pVoid, SONT_SYSVAD_POOLTAG);
    }
}
#endif // _NEW_DELETE_OPERATORS_
