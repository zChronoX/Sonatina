/*++

Copyright (c) Microsoft Corporation All Rights Reserved

Module Name:

    NewDelete.h

Abstract:

    Declaration of placement new and delete operators for kernel C++.
    Adapted minimally for Sonatina from Microsoft Windows-driver-samples (MS-PL).

--*/
#pragma once

#ifdef _NEW_DELETE_OPERATORS_

#ifdef __cpp_aligned_new
#include <vcruntime_new.h>
#endif // __cpp_aligned_new

#ifndef SONT_SYSVAD_POOLTAG
#define SONT_SYSVAD_POOLTAG 'anOS'
#endif

PVOID operator new
(
    size_t      iSize,
    POOL_FLAGS  poolFlags,
    ULONG       tag
);

PVOID operator new
(
    size_t      iSize,
    POOL_FLAGS  poolFlags
);

void __cdecl operator delete
(
    PVOID pVoid,
    ULONG tag
);

void __cdecl operator delete
(
    _Pre_maybenull_ __drv_freesMem(Mem) PVOID pVoid,
    _In_ size_t cbSize
);

#ifdef __cpp_aligned_new
void __cdecl operator delete
(
    _Pre_maybenull_ __drv_freesMem(Mem) PVOID pVoid,
    _In_ size_t cbSize,
    _In_ std::align_val_t cbAlign
);
#endif // __cpp_aligned_new

void __cdecl operator delete
(
    PVOID pVoid
);

void __cdecl operator delete[]
(
    _Pre_maybenull_ __drv_freesMem(Mem) PVOID pVoid,
    _In_ size_t cbSize
);

void __cdecl operator delete[]
(
    _Pre_maybenull_ __drv_freesMem(Mem) PVOID pVoid
);

#endif // _NEW_DELETE_OPERATORS_
