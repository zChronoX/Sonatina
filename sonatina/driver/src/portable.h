/* Small portability layer so the protocol files build both in the kernel and in
 * a host unit test. Define SONATINA_KERNEL for the driver build. */
#ifndef SONATINA_PORTABLE_H
#define SONATINA_PORTABLE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef SONATINA_KERNEL
#include <ntddk.h>
#define SONT_MEMCPY(dst, src, len) RtlCopyMemory((dst), (src), (len))
#define SONT_MEMSET(dst, value, len) RtlFillMemory((dst), (len), (value))
#define SONT_ALLOC(size) ExAllocatePool2(POOL_FLAG_NON_PAGED, (size), 'anOS')
#define SONT_FREE(ptr) ExFreePool(ptr)
#else
#include <stdlib.h>
#include <string.h>
#define SONT_MEMCPY(dst, src, len) memcpy((dst), (src), (len))
#define SONT_MEMSET(dst, value, len) memset((dst), (value), (len))
#define SONT_ALLOC(size) calloc(1, (size))
#define SONT_FREE(ptr) free(ptr)
#endif

#endif /* SONATINA_PORTABLE_H */
